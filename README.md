# ACPACS — Automated Car Park Access Control System

ACPACS is a car park access control prototype built for Nigerian license plates. Each gate cycle combines two checks:

- **Automatic License Plate Recognition (ALPR):** a YOLO plate detector, OpenCV preprocessing and PaddleOCR identify the vehicle.
- **Fingerprint session binding:** the fingerprint template captured at entry is stored in that vehicle's session. At exit, a 1:1 comparison checks that the same driver is leaving with the car.

The system also enforces car park capacity, bills by the hour with a ceiling function (₦500/hour, 1-hour minimum), records cashless payments and serves a live web dashboard.

The repository can run on real hardware (Arduino Uno, R307 fingerprint sensor, IR sensors, servo gate) or fully in simulation with the [Wokwi](https://wokwi.com) Arduino simulator in VS Code. In simulation, push buttons stand in for the IR sensors and the fingerprint sensor.

> The code comments refer to report sections (for example S3.3.2.2 and S4.2). These map to the project's methodology (Chapter 3) and evaluation (Chapter 4) write-up.

---

## Table of contents

- [Architecture](#architecture)
- [Repository layout](#repository-layout)
- [Requirements](#requirements)
- [Installation](#installation)
- [Running the system](#running-the-system)
- [Gate flows](#gate-flows)
- [REST API](#rest-api)
- [Database schema](#database-schema)
- [Billing](#billing)
- [Biometric verification](#biometric-verification)
- [Serial protocol](#serial-protocol)
- [Hardware and wiring](#hardware-and-wiring)
- [Evaluation tools](#evaluation-tools)
- [Configuration reference](#configuration-reference)
- [Known limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)

---

## Architecture

```
 ┌──────────────────────────┐     serial 9600 baud      ┌──────────────────────────┐
 │  Gate controller          │  (USB, or Wokwi RFC2217   │  acpacs/serial_bridge.py  │
 │  Arduino Uno / Wokwi sim  │◄────── on port 4000) ────►│  (session controller)     │
 │                           │                           │                           │
 │  IR sensors, R307 / FP    │  CAR_AT_ENTRY, FP_OK:...  │  ┌─────────────────────┐  │
 │  button, servo gate,      │ ────────────────────────► │  │ alpr_connector.py   │  │
 │  16x2 LCD, LEDs, buzzer   │  PLATE:, ENTRY, OPEN ...  │  │ YOLO → crop → warp  │  │
 │                           │ ◄──────────────────────── │  │ → CLAHE → PaddleOCR │  │
 └──────────────────────────┘                           │  └─────────────────────┘  │
                                                        └────────────┬─────────────┘
                                                                     │ HTTP (JSON)
                                                                     ▼
                                       ┌─────────────────────────────────────────────┐
                                       │  acpacs/main.py — FastAPI (port 8000)       │
                                       │  billing.py · biometrics.py · database.py   │
                                       │  SQLite: runtime/acpacs.db                  │
                                       │  /dashboard → acpacs/templates/             │
                                       └─────────────────────────────────────────────┘
```

| Layer | Component | Responsibility |
|---|---|---|
| Hardware | `firmware/gate_controller/gate_controller.ino` | Detects vehicles, captures fingerprints, drives the gate, LCD, LEDs and buzzer |
| Bridge | `acpacs/serial_bridge.py` | Runs each gate cycle: captures an image, runs ALPR, collects the fingerprint, calls the backend, sends OPEN/DENY/FULL, and logs latency |
| Vision | `acpacs/alpr_connector.py` | Turns an image into a validated `AAA111AA` plate string |
| Backend | `acpacs/main.py` | Owns session state, enforces duplicate and capacity rules, runs 1:1 verification and records payments |
| Storage | `acpacs/database.py` → `runtime/acpacs.db` | Two SQLite tables: `sessions` and `payments` |
| UI | `acpacs/templates/dashboard.html` | Live view of occupancy, parked vehicles and payments |

The backend is the single source of truth for session state. Restarting the bridge does not strand vehicles that are already parked.

---

## Repository layout

```
acpacs-sim/
├── acpacs/                       # Python package: the running system
│   ├── main.py                   #   FastAPI backend (API + dashboard route)
│   ├── database.py               #   SQLite schema and connection helper
│   ├── billing.py                #   Ceiling-based hourly billing
│   ├── biometrics.py             #   1:1 template similarity check (simulation stand-in)
│   ├── alpr_connector.py         #   YOLO + OpenCV + PaddleOCR plate reader
│   ├── serial_bridge.py          #   Arduino ↔ ALPR ↔ backend orchestrator
│   ├── paths.py                  #   Every file path, resolved from the project root
│   └── templates/dashboard.html  #   Live web dashboard
├── evaluation/                   # Measurement scripts for the evaluation chapter
│   ├── test_billing.py           #   Billing accuracy verification
│   ├── test_ocr.py               #   OCR accuracy measurement
│   ├── latency_report.py         #   Summarises results/latency_log.csv
│   └── make_figure.py            #   Builds the preprocessing pipeline figure
├── firmware/
│   ├── gate_controller/
│   │   └── gate_controller.ino   # Arduino firmware (sim and hardware builds)
│   └── build/                    # Compiled firmware (.hex/.elf) loaded by Wokwi
├── models/
│   ├── nigerian_plate_best.pt    # Trained YOLO plate detector (in use)
│   └── v2/nigerian_plate_best.pt # Earlier detector checkpoint
├── data/
│   ├── samples/                  # entry_image.webp, exit_image.webp, test001–010.webp
│   ├── ocr_test/                 # 15 held-out test images + ground_truth.csv
│   └── synthetic_dataset/        # 200 synthetic plate images + YOLO labels
├── results/                      # Evaluation outputs (tracked)
│   ├── latency_log.csv           #   Per-cycle timings written by the bridge
│   └── figures/                  #   Figure_4_2_preprocessing.png
├── runtime/                      # Created on demand, git-ignored
├── diagram.json                  # Wokwi circuit definition  ┐ must stay at the root,
├── wokwi.toml                    # Wokwi config              ┘ where Wokwi looks for them
├── requirements.txt
└── README.md
```

`runtime/` holds local state that is regenerated or private to your machine: the SQLite database `acpacs.db`, the ALPR stage images (`fig_a_original.jpg` to `fig_d_warped.jpg`, `debug_plate_crop.jpg`) and the camera captures (`entry_capture.jpg`, `exit_capture.jpg`).

All paths are defined in `acpacs/paths.py` relative to the project root, so nothing depends on the directory you launch from. The Python modules are imported as the `acpacs` package, so **run every command from the project root using `python -m`**, as shown below.

---

## Requirements

- **Python 3.12** (see `.python-version`)
- **For simulation:** VS Code with the [Wokwi Simulator extension](https://marketplace.visualstudio.com/items?itemName=wokwi.wokwi-vscode) and a Wokwi licence
- **To rebuild the firmware:** Arduino IDE or `arduino-cli` with the libraries `Servo`, `LiquidCrystal_I2C` and `Adafruit Fingerprint Sensor Library`
- **For hardware:** see [Hardware and wiring](#hardware-and-wiring)

### Python packages

All Python packages are pinned in `requirements.txt`:

| Package | Used by |
|---|---|
| `fastapi`, `uvicorn`, `pydantic` | Backend |
| `ultralytics` | YOLO plate detection |
| `paddleocr`, `paddlepaddle` | OCR |
| `opencv-python`, `numpy` | Preprocessing, camera capture |
| `pyserial` | Serial and RFC2217 link |
| `requests` | Bridge → backend HTTP |
| `matplotlib` | `evaluation/make_figure.py` only |

---

## Installation

```bash
git clone <repo-url> acpacs-sim
cd acpacs-sim

python3.12 -m venv env
source env/bin/activate            # Windows: env\Scripts\activate

pip install -r requirements.txt
```

The database is created automatically (`runtime/acpacs.db`) when the backend starts. To create it manually:

```bash
python -m acpacs.database
```

> **Schema upgrades:** v2.0 changed the schema (BLOB templates, `payments` replaces `transactions`) and has no migration path. If you have an `acpacs.db` from v1.x, delete it before starting.

The first time PaddleOCR runs, it downloads its models. This needs an internet connection and takes a while.

---

## Running the system

The full system runs in three terminals or panes.

### 1. Start the backend

```bash
uvicorn acpacs.main:app --reload --port 8000
```

- Dashboard: <http://localhost:8000/dashboard>
- Interactive API docs (Swagger): <http://localhost:8000/docs>

### 2a. Simulation: start Wokwi

1. Open the project folder in VS Code.
2. Run **Wokwi: Start Simulator** from the command palette. Wokwi loads `firmware/build/gate_controller.ino.hex` and exposes the serial port on `rfc2217://localhost:4000`.
3. Keep the simulator tab **visible**. Wokwi pauses the simulation when the tab is hidden.

### 2b. Hardware: flash the board

1. In `firmware/gate_controller/gate_controller.ino`, comment out `#define WOKWI_SIM`.
2. Compile and upload to the Arduino Uno.
3. Connect the board over USB.

### 3. Start the serial bridge

**Simulation.** There is no camera, so supply test images:

```bash
python -m acpacs.serial_bridge --wokwi \
    --test-entry data/samples/entry_image.webp \
    --test-exit data/samples/exit_image.webp
```

**Hardware with a camera:**

```bash
python -m acpacs.serial_bridge --port /dev/tty.usbmodem1234 --camera 0
# or leave out --port to auto-detect /dev/tty.usbmodem*, /dev/ttyUSB*, /dev/ttyACM*
```

| Flag | Meaning |
|---|---|
| `--wokwi` | Connect to the Wokwi simulator at `rfc2217://localhost:4000` |
| `--port` | Serial port of a real board (auto-detected if omitted) |
| `--camera` | OpenCV camera index (default `0`) |
| `--test-entry` / `--test-exit` | Use a fixed image instead of the camera |

### Driving the simulation

| Wokwi control | Effect |
|---|---|
| **Green button** (ENTRY IR) | A vehicle arrives at the entry gate |
| **Red button** (EXIT IR) | A vehicle arrives at the exit gate |
| **Blue button**, short press | Fingerprint of driver #1 |
| **Blue button**, held for 1.5 s or longer | Fingerprint of a *different* driver. Use this at exit to demonstrate a `BIOMETRIC MISMATCH` denial |

A typical demo:

1. Press green, then tap blue. The gate opens and the session appears on the dashboard.
2. Press red, then hold blue for 1.5 s. Access is denied because the fingerprint doesn't match.
3. Press red, then tap blue. The fare is shown on the LCD, the gate opens and the payment is recorded.

To demonstrate **CAR PARK FULL**, set `MAX_OCCUPANCY` in `acpacs/main.py` to `1` and run two entries with different plates.

---

## Gate flows

### Entry

1. The IR sensor fires and the Arduino sends `CAR_AT_ENTRY`.
2. **Capacity pre-check** (`GET /occupancy`). If the car park is full, the bridge sends `FULL` and stops. No ALPR runs and the driver isn't prompted.
3. **ALPR** reads the plate. If no valid plate is read, the bridge sends `DENY`.
4. **Duplicate pre-check** (`GET /session/{plate}`). If the plate is already parked, the bridge sends `DENY`.
5. `PLATE:<plate>` is shown on the LCD, the serial input buffer is flushed, and `ENTRY` prompts for a fingerprint (15 s window).
6. The Arduino returns `FP_OK:<hex template>:<confidence>`.
7. `POST /entry` makes the authoritative duplicate and capacity checks, then stores the template in the session row.
8. On `200` the bridge sends `OPEN`, on `409` it sends `FULL`, and otherwise it sends `DENY`.

### Exit

1. The Arduino sends `CAR_AT_EXIT`, ALPR reads the plate and `PLATE:` is shown.
2. `EXIT` prompts for a fingerprint, and the template comes back.
3. `POST /exit` loads the **single** active session for that plate and compares its stored template with the live scan (1:1, threshold 0.80).
4. On `200` the bridge sends `BILL:<amount>`. The fare stays on the LCD for about 4 s, then the bridge sends `OPEN`.
5. On `403` (biometric mismatch) the gate stays closed and the bridge sends `DENY`.

### ALPR pipeline (`alpr_connector.read_plate_from_image`)

1. **Detect.** YOLO (`models/nigerian_plate_best.pt`, confidence ≥ 0.25) finds the plate, and the highest-confidence box is kept.
2. **Crop.** The box is cropped with 20 px padding, then the top 30% is removed (the state-name header on Nigerian plates).
3. **Perspective correction.** Canny edges and a quadrilateral contour are used to warp the plate flat.
4. **CLAHE.** Contrast is enhanced on the L channel of the LAB colour space.
5. **Resize.** The crop is scaled to 640 px wide (bicubic).
6. **OCR.** PaddleOCR reads the text.
7. **Clean and validate.** Non-alphanumeric characters are removed and the text is matched against `AAA111AA`. Positional corrections are applied: `O→0`, `I→1`, `S→5`, `B→8`, `G→6`, `Z→2` in the digit positions, and the reverse in the letter positions.
8. **Reject.** Text that doesn't match the pattern, or has OCR confidence below 0.50, is rejected.

---

## REST API

Base URL: `http://localhost:8000`

| Method | Path | Description |
|---|---|---|
| `POST` | `/entry` | Start a session and bind the fingerprint template to the plate |
| `POST` | `/exit` | Run 1:1 verification, close the session and raise the bill |
| `GET` | `/occupancy` | Occupancy against capacity |
| `GET` | `/sessions` | Vehicles currently parked, with running duration and estimated bill |
| `GET` | `/session/{plate}` | Active session for one plate (`404` if none) |
| `GET` | `/payments` | Completed payment records |
| `GET` | `/dashboard` | HTML dashboard |

### `POST /entry`

```json
{
  "plate": "KSF487HN",
  "fingerprint_template": "a3f1...",   // hex-encoded template
  "fingerprint_confidence": 88,        // optional
  "name": "Ada"                        // optional, has no effect on authentication
}
```

| Status | Meaning |
|---|---|
| `200` | Session created. Returns `session_id`, `entry_time`, `occupancy` and `capacity` |
| `400` | Plate already has an active session |
| `409` | `CAR PARK FULL` |
| `422` | Fingerprint template is malformed or empty |

### `POST /exit`

```json
{ "plate": "KSF487HN", "fingerprint_template": "a3f1...", "fingerprint_confidence": 88 }
```

| Status | Meaning |
|---|---|
| `200` | Returns `duration`, `billable_hours`, `amount_naira`, `payment_id` and `match_score` |
| `403` | `BIOMETRIC MISMATCH - ACCESS DENIED` (score below 0.80) |
| `404` | No active session for the plate |
| `500` | Billing error (returned as JSON, never plain text) |

### Testing without hardware

You can run the plate reader and backend together without the Arduino. The entry command generates a random fingerprint template and prints it so you can reuse it at exit:

```bash
python -m acpacs.alpr_connector --mode entry --image data/samples/entry_image.webp
python -m acpacs.alpr_connector --mode exit  --image data/samples/exit_image.webp --template <hex from entry>
```

Pass a different `--template` at exit to see the `BIOMETRIC MISMATCH` denial.

You can also call the API directly:

```bash
T=$(python -c "import os;print(os.urandom(32).hex())")

curl -X POST localhost:8000/entry -H 'Content-Type: application/json' \
     -d "{\"plate\":\"KSF487HN\",\"fingerprint_template\":\"$T\"}"

curl localhost:8000/occupancy

curl -X POST localhost:8000/exit -H 'Content-Type: application/json' \
     -d "{\"plate\":\"KSF487HN\",\"fingerprint_template\":\"$T\"}"
```

---

## Database schema

SQLite file `runtime/acpacs.db`, created by `acpacs.database.init_db()`.

**`sessions`** (one row per parking visit)

| Column | Type | Notes |
|---|---|---|
| `id` | INTEGER PK | |
| `plate` | TEXT | Normalised to uppercase with spaces removed |
| `fingerprint_template` | BLOB | Template captured at entry |
| `name` | TEXT, nullable | Optional driver name |
| `entry_time` / `exit_time` | TEXT | `YYYY-MM-DDTHH:MM:SS` |
| `status` | TEXT | `active` or `closed` |

A partial unique index, `idx_one_active_session ON sessions(plate) WHERE status = 'active'`, guarantees at the database level that a plate has at most one active session. This also closes the check-then-insert race when there are several lanes.

**`payments`** (one row per closed session)

| Column | Type |
|---|---|
| `id` | INTEGER PK |
| `session_id` | INTEGER, FK → `sessions.id` |
| `plate`, `entry_time`, `exit_time` | TEXT |
| `duration_minutes`, `billable_hours` | INTEGER |
| `fee_amount` | REAL |
| `payment_mode` | TEXT (default `CASHLESS`) |
| `paid_at` | TEXT |

---

## Billing

`acpacs/billing.py`:

```
duration_minutes = max(1, floor((exit - entry) / 60 s))
billable_hours   = ceil(duration_minutes / 60)
fee              = billable_hours × RATE_PER_HOUR_NAIRA      (default ₦500)
```

| Stay | Billable hours | Fee |
|---|---|---|
| 40 s | 1 | ₦500 |
| 30 min | 1 | ₦500 |
| 60 min | 1 | ₦500 |
| 61 min | 2 | ₦1,000 |
| 80 min | 2 | ₦1,000 |

Stays shorter than a minute are rounded up to 1 minute, so the 1-hour minimum always applies. An exit time earlier than the entry time raises `ValueError`, which the API returns as a JSON `500`.

---

## Biometric verification

At exit, ACPACS performs **1:1 verification**, not 1:N identification. It loads one session row, the one for the plate just read, and compares that row's stored template with the live scan. The cost of this check doesn't grow with occupancy, and nothing is enrolled on the sensor's onboard library.

- **Hardware design intent:** the stored template is pushed back to the R307 (`DownChar`), and the sensor's `Match` instruction returns the score.
- **Simulation:** `biometrics.similarity()` returns the fraction of bits that agree between two equal-length templates. Identical templates score 1.0, and unrelated random templates score about 0.5. A match requires `MATCH_THRESHOLD = 0.80`.

> ⚠️ The simulated score is a byte-level similarity, **not** minutiae matching. It reproduces the threshold *logic*. It doesn't measure biometric performance, so its output must not be reported as FAR/FRR.

The firmware produces deterministic synthetic 32-byte templates, seeded by the simulated person's ID. A real R307 template is 512 bytes.

---

## Serial protocol

9600 baud, newline-terminated ASCII.

**Python → Arduino**

| Command | Effect |
|---|---|
| `PLATE:<plate>` | Shows the plate on the LCD |
| `ENTRY` / `EXIT` | Prompts for a fingerprint (15 s in sim, 3 s on hardware) |
| `OPEN` | Green LED on, servo to 90° for 5 s, then closes |
| `DENY` | Red LED, 3 beeps, "Access Denied" |
| `FULL` | Red LED, "CAR PARK FULL" |
| `BILL:<amount>` | Shows "Bill: N<amount>" for 3 s |

**Arduino → Python**

| Message | Meaning |
|---|---|
| `READY` | Firmware initialised |
| `CAR_AT_ENTRY` / `CAR_AT_EXIT` | IR sensor triggered (2 s debounce) |
| `FP_OK:<hex>:<confidence>` | Template captured |
| `FP_FAIL` | No finger presented within the time window |

The bridge waits 20 s for a fingerprint, which is longer than the firmware's 15 s window. The firmware therefore always reports its own `FP_FAIL`, and no command lands in a buffer the Arduino isn't reading yet.

---

## Hardware and wiring

| Component | Arduino Uno pin |
|---|---|
| Entry IR sensor (active LOW) | D4 |
| Exit IR sensor (active LOW) | D8 |
| R307 fingerprint sensor | D2 (RX), D3 (TX) via SoftwareSerial at 57600 baud |
| Servo (gate barrier) | D9 |
| Green LED (granted) | D5 (220 Ω) |
| Red LED (denied) | D6 (220 Ω) |
| Buzzer | D7 |
| 16×2 LCD, I²C address `0x27` | SDA A4, SCL A5 |
| Fingerprint button (**simulation only**) | D12 |

The Wokwi circuit in `diagram.json` reproduces this wiring, with push buttons standing in for the IR sensors and the fingerprint sensor.

**Switching between simulation and hardware:** `#define WOKWI_SIM` at the top of the firmware controls the fingerprint source (button or R307), the scan timeout and the buzzer mode (`tone()` for Wokwi's passive buzzer). Comment the line out before flashing real hardware.

---

## Evaluation tools

These scripts produce the measurements for the evaluation chapter (targets from S4.2).

### Billing accuracy (target: 100%)

```bash
python -m evaluation.test_billing
```

Checks the ceiling formula against hand-computed fees, concentrating on the hour boundaries, and confirms that an exit before entry is rejected.

### OCR accuracy (target: ≥ 90% character accuracy)

```bash
python -m evaluation.test_ocr
# or with another test set:
python -m evaluation.test_ocr --images DIR --truth DIR/ground_truth.csv
```

Reports both the **exact-match rate** (the figure that matters operationally) and the **mean character accuracy** (`1 − edit distance / length`). The test images must not have been used to train the detector.

### Latency (target: ≤ 2.5 s system latency)

Every successful gate cycle run by the serial bridge appends a row to `results/latency_log.csv`:

```
timestamp, event, plate, alpr_s, backend_s, driver_wait_s, system_s, total_s
```

`system_s` excludes the time the driver takes to present a finger and, for exits, the 4 s fare display. Summarise the log with:

```bash
python -m evaluation.latency_report
```

Quote the system figure and the end-to-end figure together, each labelled.

### Preprocessing figure

```bash
python -m evaluation.make_figure [--image data/samples/entry_image.webp] [--out FILE] [--keep-stages]
```

Runs the real pipeline functions and saves a 300 dpi figure showing each stage to `results/figures/Figure_4_2_preprocessing.png`. `--keep-stages` also writes each stage there as `stage_*.jpg`.

### Datasets

- `data/synthetic_dataset/` contains 200 synthetic Nigerian plate images (`synthetic_NNNN_<PLATE>.jpg`) with YOLO-format labels (`class cx cy w h`, normalised).
- `data/ocr_test/` contains 15 held-out photos (`fig01.jpg` to `fig15.jpg`) with `ground_truth.csv` (`filename,plate`).

---

## Configuration reference

| Setting | File | Default | Purpose |
|---|---|---|---|
| `MAX_OCCUPANCY` | `acpacs/main.py` | `5` | Number of bays |
| `RATE_PER_HOUR_NAIRA` | `acpacs/billing.py` | `500.0` | Hourly rate |
| `MATCH_THRESHOLD` | `acpacs/biometrics.py` | `0.80` | 1:1 verification threshold |
| `DB_PATH` | `acpacs/paths.py` | `runtime/acpacs.db` | SQLite file |
| `MODEL_PATH` | `acpacs/paths.py` | `models/nigerian_plate_best.pt` | YOLO weights |
| `LATENCY_LOG` | `acpacs/paths.py` | `results/latency_log.csv` | Gate-cycle timings |
| `CONFIDENCE_THRESHOLD` | `acpacs/alpr_connector.py` | `0.25` | YOLO detection confidence |
| `MIN_OCR_CONFIDENCE` | `acpacs/alpr_connector.py` | `0.50` | Minimum OCR confidence |
| `API_BASE_URL` | `acpacs/alpr_connector.py`, `acpacs/serial_bridge.py` | `http://localhost:8000` | Backend URL |
| `WOKWI_PORT` | `acpacs/serial_bridge.py` | `rfc2217://localhost:4000` | Must match `wokwi.toml` |
| `FP_TIMEOUT` | `acpacs/serial_bridge.py` | `20` s | Bridge wait for a fingerprint |
| `WOKWI_SIM` | `firmware/gate_controller/gate_controller.ino` | defined | Simulation or hardware build |
| `FP_TEMPLATE_BYTES` | `firmware/gate_controller/gate_controller.ino` | `32` | Synthetic template size |

---

## Known limitations

- **Simulated biometrics.** `acpacs/biometrics.py` and the firmware's synthetic templates stand in for the R307. The hardware path doesn't yet read a real 512-byte template with `UpChar`; it derives a placeholder from the matched sensor slot (see the `TODO` in `scanFingerprint()`).
- **Template size.** The simulation uses 32-byte templates, because sending 512-byte templates as hex over SoftwareSerial is impractical on the Uno's 2 KB of SRAM.
- **Plate format.** Only the standard `AAA111AA` Nigerian format is accepted.
- **Payments.** Payments are recorded as `CASHLESS`. No payment gateway is integrated.
- **Single camera.** Entry and exit share one camera index on hardware.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ModuleNotFoundError: No module named 'acpacs'` | Run from the project root with `python -m ...`, not `python acpacs/file.py` |
| `Could not connect to rfc2217://localhost:4000` | Start the Wokwi simulation, keep its tab visible, and check `rfc2217ServerPort = 4000` in `wokwi.toml` |
| "No READY seen" on Wokwi | This is normal. The simulator booted before the bridge connected |
| `Backend not reachable` | Start `uvicorn acpacs.main:app --port 8000` |
| `HTTP 500 (not JSON)` in the bridge | Look in the uvicorn terminal for the traceback |
| Old-schema errors (`fingerprint_id`, `transactions`) | Delete `runtime/acpacs.db` and restart the backend |
| `No plate detected` | Check the image and `MODEL_PATH`. The intermediate crops are saved in `runtime/` as `fig_*.jpg` and `debug_plate_crop.jpg` |
| Slow first ALPR run | YOLO and PaddleOCR load and download models on first use |
| Exit always denied | Use a short press for the same driver. A press held for 1.5 s or longer produces a different template |
