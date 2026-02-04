from __future__ import annotations

from datetime import date

import pytest

from kaira.utils.ids import instrument_id, option_id


def test_option_id_normalizes_and_validates() -> None:
    assert option_id("nifty", date(2026, 2, 5), 18000, "c") == "NIFTY|2026-02-05|18000|C"
    assert option_id("BANKNIFTY", date(2026, 2, 5), 42000, "P") == "BANKNIFTY|2026-02-05|42000|P"

    with pytest.raises(ValueError):
        option_id("NIFTY", date(2026, 2, 5), 18000, "X")


def test_instrument_id_is_stable() -> None:
    a = instrument_id("NIFTY", date(2026, 2, 5), 18000, "C")
    b = instrument_id("NIFTY", date(2026, 2, 5), 18000, "C")
    c = instrument_id("NIFTY", date(2026, 2, 5), 18050, "C")
    assert a == b
    assert a != c

