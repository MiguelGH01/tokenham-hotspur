from national_id import is_valid_national_id


def test_dni_and_nie():
    assert is_valid_national_id("12345678Z")
    assert not is_valid_national_id("12345678A")
    assert is_valid_national_id("X1234567L")
    assert is_valid_national_id(" 12345678-z ")
