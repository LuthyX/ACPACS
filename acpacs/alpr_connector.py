"""
ACPACS — ALPR Connector (Section 3.2 + 3.3 bridge)

This script is the glue between the ALPR pipeline and the FastAPI backend.
It:
  1. Takes an image (from a camera or file)
  2. Runs YOLO plate detection
  3. Crops and preprocesses the plate (CLAHE + perspective correction)
  4. Runs PaddleOCR to read the plate text
  5. Calls the backend API (/entry or /exit) with the result

Usage (from the project root):
  python -m acpacs.alpr_connector --mode entry --image path/to/image.jpg
  python -m acpacs.alpr_connector --mode exit  --image path/to/image.jpg --template HEX
"""

import cv2
import numpy as np
import requests
import argparse
import os
import re
from ultralytics import YOLO
from paddleocr import PaddleOCR

from acpacs.paths import MODEL_PATH, RUNTIME_DIR

# --- Configuration ---
API_BASE_URL = "http://localhost:8000"
CONFIDENCE_THRESHOLD = 0.25
PLATE_PATTERN = re.compile(r'^[A-Z]{3}[0-9]{3}[A-Z]{2}$')
MIN_OCR_CONFIDENCE = 0.50

# --- Load models once at startup ---
print("Loading YOLO model...")
detector = YOLO(str(MODEL_PATH))

print("Loading OCR engine...")
ocr_engine = PaddleOCR(
    use_textline_orientation=True,
    lang='en',
    enable_mkldnn=False
)


# -------------------------------------------------------------------
# PREPROCESSING: Perspective Correction
# -------------------------------------------------------------------
def perspective_correction(plate_crop):
    gray = cv2.cvtColor(plate_crop, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (5, 5), 0)
    edges = cv2.Canny(blurred, 50, 150)
    contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    if not contours:
        return plate_crop

    largest = max(contours, key=cv2.contourArea)
    peri = cv2.arcLength(largest, True)
    approx = cv2.approxPolyDP(largest, 0.02 * peri, True)

    if len(approx) != 4:
        return plate_crop

    pts = approx.reshape(4, 2).astype(np.float32)
    rect = order_corners(pts)

    w = int(max(
        np.linalg.norm(rect[1] - rect[0]),
        np.linalg.norm(rect[2] - rect[3])
    ))
    h = int(max(
        np.linalg.norm(rect[3] - rect[0]),
        np.linalg.norm(rect[2] - rect[1])
    ))

    dst = np.array([[0, 0], [w, 0], [w, h], [0, h]], dtype=np.float32)
    M = cv2.getPerspectiveTransform(rect, dst)
    warped = cv2.warpPerspective(plate_crop, M, (w, h))
    return warped


def order_corners(pts):
    rect = np.zeros((4, 2), dtype=np.float32)
    s = pts.sum(axis=1)
    rect[0] = pts[np.argmin(s)]
    rect[2] = pts[np.argmax(s)]
    diff = np.diff(pts, axis=1)
    rect[1] = pts[np.argmin(diff)]
    rect[3] = pts[np.argmax(diff)]
    return rect


# -------------------------------------------------------------------
# PREPROCESSING: CLAHE Contrast Enhancement
# -------------------------------------------------------------------
def apply_clahe(plate_crop):
    lab = cv2.cvtColor(plate_crop, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    l_enhanced = clahe.apply(l)
    enhanced_lab = cv2.merge([l_enhanced, a, b])
    enhanced_bgr = cv2.cvtColor(enhanced_lab, cv2.COLOR_LAB2BGR)
    return enhanced_bgr

def correct_plate_chars(text):
    """Apply positional corrections — numbers only at positions 3,4,5"""
    if len(text) != 8:
        return text

    number_fixes = {'O': '0', 'I': '1', 'S': '5', 'B': '8', 'G': '6', 'Z': '2'}
    letter_fixes = {'0': 'O', '1': 'I', '5': 'S', '8': 'B'}

    result = list(text)
    for i in [3, 4, 5]:      # must be numbers
        result[i] = number_fixes.get(result[i], result[i])
    for i in [0, 1, 2, 6, 7]:  # must be letters
        result[i] = letter_fixes.get(result[i], result[i])

    return ''.join(result)


def clean_plate_text(raw_text):
    cleaned = re.sub(r'[^A-Z0-9]', '', raw_text.upper())
    print(f"   Cleaned OCR: {cleaned} ({len(cleaned)} chars)")

    # Try exact pattern match first
    match = re.search(r'[A-Z]{3}[0-9]{3}[A-Z]{2}', cleaned)
    if match:
        return correct_plate_chars(match.group())

    # Apply corrections then try pattern again
    if len(cleaned) >= 8:
        # Try every 8-char window
        for start in range(len(cleaned) - 7):
            window = cleaned[start:start+8]
            corrected = correct_plate_chars(window)
            if re.match(r'^[A-Z]{3}[0-9]{3}[A-Z]{2}$', corrected):
                return corrected

    return cleaned



# -------------------------------------------------------------------
# CORE PIPELINE: Image → Plate Text
# -------------------------------------------------------------------
def read_plate_from_image(image_path):
    img = cv2.imread(image_path)
    if img is None:
        print(f"❌ Could not read image: {image_path}")
        return None, 0
    cv2.imwrite(str(RUNTIME_DIR / "fig_a_original.jpg"), img)

    # Step 1 — Detect plate with YOLO
    results = detector.predict(image_path, conf=CONFIDENCE_THRESHOLD, verbose =False)
    boxes = results[0].boxes.xyxy.cpu().numpy()

    if len(boxes) == 0:
        print("❌ No plate detected in image")
        return None, 0

    confidences = results[0].boxes.conf.cpu().numpy()
    best_idx = confidences.argmax()
    x1, y1, x2, y2 = boxes[best_idx].astype(int)
    _ann = img.copy()
    cv2.rectangle(_ann, (x1, y1), (x2, y2), (0, 255, 0), 4)
    cv2.imwrite(str(RUNTIME_DIR / "fig_b_detected.jpg"), _ann)

    print(f"✅ Plate detected (conf: {confidences[best_idx]:.2%}) at [{x1},{y1},{x2},{y2}]")

    # Step 2 — Crop with padding
    pad = 20
    h_img, w_img = img.shape[:2]
    x1p = max(0, x1 - pad)
    y1p = max(0, y1 - pad)
    x2p = min(w_img, x2 + pad)
    y2p = min(h_img, y2 + pad)
    plate_crop = img[y1p:y2p, x1p:x2p]

    # Step 2.5 — Remove top portion (state name header)
    # Nigerian plates have state name in top ~30% — skip it for OCR
    h_crop = plate_crop.shape[0]
    plate_crop = plate_crop[int(h_crop * 0.30):, :]
    cv2.imwrite(str(RUNTIME_DIR / "fig_c_cropped.jpg"), plate_crop)

    # Step 3 — Perspective correction
    plate_crop = perspective_correction(plate_crop)
    cv2.imwrite(str(RUNTIME_DIR / "fig_d_warped.jpg"), plate_crop)

    # Step 4 — CLAHE contrast enhancement
    plate_crop = apply_clahe(plate_crop)

     # Step 4.5 — Resize to standard width for better OCR
    target_width = 640
    h_crop, w_crop = plate_crop.shape[:2]
    scale = target_width / w_crop
    plate_crop = cv2.resize(plate_crop, (target_width, int(h_crop * scale)),
                            interpolation=cv2.INTER_CUBIC)

    # Save preprocessed crop for debugging
    debug_path = str(RUNTIME_DIR / "debug_plate_crop.jpg")
    cv2.imwrite(debug_path, plate_crop)
    print(f"   Preprocessed crop saved to: {debug_path}")

    # Step 5 — OCR. Pass the array directly; the debug JPEG above is kept
    # for the report, but re-reading it from disk is wasted I/O.
    try:
        ocr_result = ocr_engine.ocr(plate_crop)
    except Exception:
        ocr_result = ocr_engine.ocr(debug_path)   # fallback

    raw_text = ""
    best_conf = 0

    if ocr_result and isinstance(ocr_result[0], dict):
        res_dict = ocr_result[0]
        for text, conf in zip(res_dict.get('rec_texts', []),
                               res_dict.get('rec_scores', [])):
            raw_text += text
            best_conf = max(best_conf, conf)
    elif ocr_result and isinstance(ocr_result[0], list):
        for line in ocr_result[0]:
            if line:
                raw_text += line[1][0]
                best_conf = max(best_conf, line[1][1])

    if not raw_text:
        print("❌ OCR could not read plate text")
        return None, 0

    # Step 6 — Clean and pattern-match
    plate_text = clean_plate_text(raw_text)

    print(f"   Raw OCR output: {raw_text.upper()}")
    print(f"✅ Plate text: {plate_text} (OCR conf: {best_conf:.2%})")

    if not PLATE_PATTERN.match(plate_text):
        print(f"   [reject] '{plate_text}' is not a valid AAA111AA plate")
        return None, 0

    if best_conf < MIN_OCR_CONFIDENCE:
        print(f"   [reject] OCR confidence {best_conf:.1%} below threshold")
        return None, 0

    return plate_text, best_conf


# -------------------------------------------------------------------
# BACKEND INTEGRATION
# -------------------------------------------------------------------
# Standalone path for testing ALPR + backend without the Arduino. The
# fingerprint template is supplied on the command line (hex), or generated
# at random on entry so the same value can be replayed at exit. This is the
# same 32-byte stand-in the Wokwi firmware emits (FP_TEMPLATE_BYTES).
SIM_TEMPLATE_BYTES = 32


def _post(endpoint, payload):
    """POST to the backend. Returns (data, error) — exactly one is None."""
    try:
        response = requests.post(f"{API_BASE_URL}{endpoint}", json=payload, timeout=5)
    except requests.ConnectionError:
        print("❌ Could not connect to backend — is the server running?")
        return None, "Backend not reachable"
    except requests.Timeout:
        print("❌ Backend timeout")
        return None, "Backend timeout"

    try:
        body = response.json()
    except ValueError:
        body = {"detail": f"HTTP {response.status_code} (not JSON)"}

    if response.status_code == 200:
        return body, None

    error = body.get("detail", "Unknown error")
    print(f"\n❌ Backend error: {error}")
    return None, error


def process_entry(image_path, fingerprint_template=None):
    print(f"\n{'='*40}")
    print("PROCESSING: VEHICLE ENTRY")
    print(f"{'='*40}")

    plate, confidence = read_plate_from_image(image_path)
    if not plate:
        return {"success": False, "error": "Could not read plate"}

    if not fingerprint_template:
        fingerprint_template = os.urandom(SIM_TEMPLATE_BYTES).hex()
        print(f"   Simulated fingerprint template: {fingerprint_template}")

    data, error = _post("/entry", {
        "plate": plate,
        "fingerprint_template": fingerprint_template
    })
    if error:
        return {"success": False, "error": error}

    print(f"\n✅ ENTRY RECORDED")
    print(f"   Plate: {data['plate']}")
    print(f"   Session ID: {data['session_id']}")
    print(f"   Entry time: {data['entry_time']}")
    print(f"   Occupancy: {data['occupancy']}/{data['capacity']}")
    return {"success": True, "data": data, "fingerprint_template": fingerprint_template}


def process_exit(image_path, fingerprint_template):
    print(f"\n{'='*40}")
    print("PROCESSING: VEHICLE EXIT")
    print(f"{'='*40}")

    plate, confidence = read_plate_from_image(image_path)
    if not plate:
        return {"success": False, "error": "Could not read plate"}

    data, error = _post("/exit", {
        "plate": plate,
        "fingerprint_template": fingerprint_template
    })
    if error:
        return {"success": False, "error": error}

    print(f"\n✅ EXIT RECORDED")
    print(f"   Plate: {data['plate']}")
    print(f"   Match score: {data['match_score']:.2f}")
    print(f"   Duration: {data['duration']}")
    print(f"   Hours billed: {data['billable_hours']}")
    print(f"   Amount: ₦{data['amount_naira']:,.0f}")
    return {"success": True, "data": data}


# -------------------------------------------------------------------
# CLI INTERFACE
# -------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="ACPACS ALPR Connector")
    parser.add_argument("--mode", choices=["entry", "exit"], required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--template", default=None,
                        help="hex fingerprint template (generated on entry if omitted)")
    args = parser.parse_args()

    if args.mode == "entry":
        result = process_entry(args.image, args.template)
        if result["success"]:
            print(f"\n💾 Use this template for exit: --template {result['fingerprint_template']}")
    elif args.mode == "exit":
        if not args.template:
            print("❌ --template is required for exit mode")
        else:
            process_exit(args.image, args.template)
