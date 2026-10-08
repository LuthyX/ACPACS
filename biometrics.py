"""
biometrics.py
ACPACS — 1:1 Template Verification (report S3.3.2.2)

Report S3.3.2.2 specifies that exit authentication performs a single
pairwise comparison between the live scan and the template stored in the
session row, producing a match score which is tested against a threshold.

On real hardware that comparison is done by the R307 itself: the stored
template is pushed back to the sensor with DownChar, the live scan sits in
the other character buffer, and the Match instruction returns a score. The
host never implements the matching algorithm.

In simulation there is no sensor, so this module stands in for it. The
comparison below is a byte-level similarity ratio, NOT minutiae matching.
It reproduces the SEMANTICS the report describes — a score tested against
a threshold, rather than exact equality — so that the surrounding logic is
identical to the hardware case. It does not reproduce the biometrics.

Chapter 4 must state this plainly. Do not present the numbers this module
produces as FAR/FRR measurements; they are not.
"""

# Report S3.3.2.2 sets the sensor to Security Level 3.
MATCH_THRESHOLD = 0.80


def similarity(a: bytes, b: bytes) -> float:
    """
    Score two templates from 0.0 to 1.0.

    Identical templates score 1.0. Two unrelated templates score around
    0.5, because independent random bytes agree on about half their bits.
    That gap is what makes a 0.80 threshold meaningful here.
    """
    if not a or not b or len(a) != len(b):
        return 0.0

    agreeing_bits = sum(8 - bin(x ^ y).count("1") for x, y in zip(a, b))
    return agreeing_bits / (len(a) * 8)


def verify(stored: bytes, live: bytes) -> tuple[bool, float]:
    """
    1:1 verification. Returns (passed, score).

    This is the single question S3.3.2.2 poses: is this the same finger
    that was bound to this session at entry?
    """
    score = similarity(stored, live)
    return score >= MATCH_THRESHOLD, score