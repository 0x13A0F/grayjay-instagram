"""Tests for the Instagram shortcode <-> pk codec in utils.

Run from the backend/ dir (PYTHONPATH points at the code in server/):
    PYTHONPATH=server python3 -m tests.test_shortcode
"""

from utils import shortcode_to_pk, pk_to_shortcode

passed = 0


def check(name, cond):
    global passed
    assert cond, "FAILED: " + name
    passed += 1
    print("  ok:", name)


# Round-trip real shortcodes: decode -> re-encode should return the original.
for code in ["DAJu3Kis4vq", "CqIbCzYMi5C", "DW-toGVj4I4"]:
    pk = shortcode_to_pk(code)
    check(f"round-trip {code}", pk_to_shortcode(pk) == code)

# Known reference from a real fixture.
check("DAJu3Kis4vq -> known pk",
      shortcode_to_pk("DAJu3Kis4vq") == 3461503889641278442)

# A 19-digit pk: exactly the case JS Number can't represent (> 2**53),
# which is why this lives server-side.
check("pk exceeds JS safe integer",
      shortcode_to_pk("DAJu3Kis4vq") > 2 ** 53)

# Trailing chars outside the alphabet are ignored (some URLs carry a suffix).
check("stops at non-alphabet char",
      shortcode_to_pk("DAJu3Kis4vq") == shortcode_to_pk("DAJu3Kis4vq/"))

print(f"\nALL {passed} CHECKS PASSED")
