"""
test_ocr.py
ACPACS — OCR Accuracy Measurement (report metric S4.2)

S4.2 targets >= 90% character accuracy. Nothing in the project measures
this: YOLO's mAP tells you the plate was FOUND, not that PaddleOCR READ
it correctly. This closes that gap.

SETUP
-----
1. Put your test images in a folder, e.g. ./ocr_test/
2. Create ground_truth.csv next to this script:

       filename,plate
       car01.jpg,KSF487HN
       car02.jpg,ABC123XY
       car03.jpg,LSD901GH

   Use the TRUE plate as a human reads it from the photo. Every image
   must be one the YOLO model never trained on, or the number is
   meaningless.

3. python3 test_ocr.py --images ./ocr_test --truth ground_truth.csv

WHAT IT REPORTS
---------------
Exact match rate   — plates read perfectly, character for character.
                     This is the figure that matters operationally: a
                     plate that is 7/8 correct still fails to match a
                     session at the exit gate.
Character accuracy — 1 - (edit distance / true length), averaged. This is
                     what S4.2's ">= 90% character accuracy" refers to.

Report both. Character accuracy alone flatters the system, because near
misses score well while being operationally useless.
"""

import argparse
import csv
import os
import sys


def edit_distance(a, b):
    """Levenshtein distance. No third-party dependency needed."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(
                previous[j] + 1,          # deletion
                current[j - 1] + 1,       # insertion
                previous[j - 1] + (ca != cb)  # substitution
            ))
        previous = current
    return previous[-1]


def char_accuracy(truth, predicted):
    """Fraction of characters correct, allowing for insertions/deletions."""
    if not truth:
        return 0.0
    return max(0.0, 1.0 - edit_distance(truth, predicted) / len(truth))


def main():
    ap = argparse.ArgumentParser(description="ACPACS OCR accuracy measurement")
    ap.add_argument("--images", default="./ocr_test", help="folder of test images")
    ap.add_argument("--truth", default="ground_truth.csv", help="filename,plate CSV")
    args = ap.parse_args()

    if not os.path.exists(args.truth):
        print(f"No ground truth file at {args.truth}. See the docstring.")
        return 1

    with open(args.truth, newline="") as f:
        cases = [r for r in csv.DictReader(f) if r.get("filename")]

    if not cases:
        print(f"{args.truth} has no rows.")
        return 1

    # Imported here so the ground-truth checks above fail fast, before
    # spending 30 seconds loading YOLO and PaddleOCR.
    from alpr_connector import read_plate_from_image

    print(f"\nACPACS OCR Accuracy — {len(cases)} test images")
    print("Target (S4.2): >= 90% character accuracy\n")

    header = f"{'Image':<20}{'Expected':<12}{'Read':<12}{'Chars':>8}{'Exact':>8}"
    print(header)
    print("-" * len(header))

    accuracies = []
    exact = 0
    failed = 0

    for case in cases:
        path = os.path.join(args.images, case["filename"])
        truth = case["plate"].strip().upper()

        if not os.path.exists(path):
            print(f"{case['filename']:<20}{truth:<12}{'FILE MISSING':<12}"
                  f"{'-':>8}{'-':>8}")
            failed += 1
            continue

        try:
            predicted, _ = read_plate_from_image(path)
        except Exception as e:
            print(f"{case['filename']:<20}{truth:<12}{'ERROR':<12}{'-':>8}{'-':>8}")
            print(f"    {type(e).__name__}: {e}")
            failed += 1
            continue

        predicted = (predicted or "").strip().upper()
        acc = char_accuracy(truth, predicted)
        is_exact = predicted == truth

        accuracies.append(acc)
        exact += is_exact

        print(f"{case['filename']:<20}{truth:<12}{predicted or '(none)':<12}"
              f"{acc:>7.1%}{'YES' if is_exact else 'no':>8}")

    print("-" * len(header))

    n = len(accuracies)
    if n == 0:
        print("\nNo images were processed successfully.")
        return 1

    mean_acc = sum(accuracies) / n
    print(f"\nImages processed:     {n}"
          + (f"  ({failed} skipped or errored)" if failed else ""))
    print(f"Exact match rate:     {exact}/{n} = {exact / n:.1%}")
    print(f"Character accuracy:   {mean_acc:.1%}  "
          f"({'PASS' if mean_acc >= 0.90 else 'BELOW TARGET'}, target 90%)")

    if n < 15:
        print(f"\nNote: {n} images is a small sample. State the sample size")
        print("in Chapter 4 alongside the percentage.")

    return 0


if __name__ == "__main__":
    sys.exit(main())