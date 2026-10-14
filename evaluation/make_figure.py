"""
make_figure.py
ACPACS — generates Figure 4.2 (license plate preprocessing pipeline)

Reproduces the five stages of the recognition pipeline on a single image and
assembles them into one labelled figure ready to insert into Chapter Four.

It imports the real preprocessing functions from alpr_connector, so the
panels show exactly what the pipeline does rather than a reimplementation.

    python -m evaluation.make_figure --image data/samples/entry_image.webp

Output: results/figures/Figure_4_2_preprocessing.png (300 dpi), plus the
individual stage files with --keep-stages, in case you would rather lay
them out yourself.

Run it from the project root. Loading YOLO and PaddleOCR takes a little
time on first import.
"""

import argparse
import os
import sys

import cv2
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.gridspec import GridSpec

# The pipeline's own functions, so the figure cannot drift from the code.
from acpacs.paths import FIGURES_DIR, SAMPLES_DIR
from acpacs.alpr_connector import (
    detector,
    perspective_correction,
    apply_clahe,
    CONFIDENCE_THRESHOLD,
)


def bgr2rgb(img):
    return cv2.cvtColor(img, cv2.COLOR_BGR2RGB)


def build_stages(image_path, pad=20, header_fraction=0.30, target_width=640):
    """Re-run the preprocessing stages, returning each intermediate image."""
    img = cv2.imread(image_path)
    if img is None:
        sys.exit(f"Could not read {image_path}")

    stages = {"a_original": img.copy()}

    # --- stage b: detection ---
    results = detector.predict(img, conf=CONFIDENCE_THRESHOLD, verbose=False)
    boxes = results[0].boxes.xyxy.cpu().numpy()
    if len(boxes) == 0:
        sys.exit("No plate detected in this image. Try a different one.")

    confs = results[0].boxes.conf.cpu().numpy()
    best = confs.argmax()
    x1, y1, x2, y2 = boxes[best].astype(int)
    print(f"  detected at [{x1},{y1},{x2},{y2}] conf {confs[best]:.2%}")

    annotated = img.copy()
    thickness = max(2, int(min(img.shape[:2]) * 0.006))
    cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 255, 0), thickness)
    stages["b_detected"] = annotated

    # --- stage c: padded crop with the state band removed ---
    h_img, w_img = img.shape[:2]
    crop = img[max(0, y1 - pad):min(h_img, y2 + pad),
               max(0, x1 - pad):min(w_img, x2 + pad)]
    crop = crop[int(crop.shape[0] * header_fraction):, :]
    stages["c_cropped"] = crop.copy()

    # --- stage d: perspective correction ---
    warped = perspective_correction(crop)
    stages["d_warped"] = warped.copy()
    if warped.shape == crop.shape and (warped == crop).all():
        print("  note: no quadrilateral found, so stage (d) is unchanged from (c)")
        print("        try an image taken at more of an angle if you want the")
        print("        warp to be visible in the figure")

    # --- stage e: CLAHE, then the standard resize ---
    enhanced = apply_clahe(warped)
    scale = target_width / enhanced.shape[1]
    enhanced = cv2.resize(enhanced, (target_width, int(enhanced.shape[0] * scale)),
                          interpolation=cv2.INTER_CUBIC)
    stages["e_enhanced"] = enhanced

    return stages


def compose(stages, out_path):
    """Two full images on top, the three plate crops as strips below."""
    fig = plt.figure(figsize=(9.0, 8.4))
    gs = GridSpec(4, 2, figure=fig,
                  height_ratios=[3.1, 1, 1, 1], hspace=0.32, wspace=0.06)

    plt.rcParams["font.family"] = "DejaVu Serif"

    def panel(ax, img, label):
        ax.imshow(bgr2rgb(img))
        ax.set_xticks([]); ax.set_yticks([])
        for s in ax.spines.values():
            s.set_edgecolor("#333333"); s.set_linewidth(0.8)
        ax.set_xlabel(label, fontsize=9.5, labelpad=6)

    panel(fig.add_subplot(gs[0, 0]), stages["a_original"], "(a) Original image")
    panel(fig.add_subplot(gs[0, 1]), stages["b_detected"],
          "(b) YOLOv8 detection")
    panel(fig.add_subplot(gs[1, :]), stages["c_cropped"],
          "(c) Padded crop, state band removed")
    panel(fig.add_subplot(gs[2, :]), stages["d_warped"],
          "(d) Perspective corrected")
    panel(fig.add_subplot(gs[3, :]), stages["e_enhanced"],
          "(e) Contrast enhanced (CLAHE) and resized")

    plt.savefig(out_path, dpi=300, bbox_inches="tight", facecolor="white")
    print(f"\nwrote {out_path}")


def main():
    ap = argparse.ArgumentParser(description="Generate Figure 4.2")
    ap.add_argument("--image", default=str(SAMPLES_DIR / "entry_image.webp"),
                    help="source photograph (default: data/samples/entry_image.webp)")
    ap.add_argument("--out", default=str(FIGURES_DIR / "Figure_4_2_preprocessing.png"))
    ap.add_argument("--keep-stages", action="store_true",
                    help="also write each stage as its own file")
    args = ap.parse_args()

    if not os.path.exists(args.image):
        sys.exit(f"No such file: {args.image}")

    print(f"Processing {args.image} ...")
    stages = build_stages(args.image)

    if args.keep_stages:
        for name, img in stages.items():
            fn = str(FIGURES_DIR / f"stage_{name}.jpg")
            cv2.imwrite(fn, img)
            print(f"  wrote {fn}")

    compose(stages, args.out)


if __name__ == "__main__":
    main()