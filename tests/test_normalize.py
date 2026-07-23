import pytest

from normalize import (
    filter_us_locations,
    infer_work_mode,
    is_us_location,
    normalize_category,
    normalize_sponsorship,
)


@pytest.mark.parametrize(
    "location",
    [
        "San Jose, CA",
        "Carlsbad, Ca",          # feed is inconsistent about case
        "Long Island, New York",  # full state name
        "Michigan",               # bare state
        "NYC",                    # bare-city alias
        "SF",
        "Washington, DC",
        "Remote in USA",
        "United States",
        "San Juan, PR",
    ],
)
def test_us_locations_are_recognised(location):
    assert is_us_location(location)


@pytest.mark.parametrize(
    "location",
    [
        "Toronto, ON, Canada",
        "London, UK",
        "Munich, Germany",
        "Chennai, Tamil Nadu, India",
        "Dubai - United Arab Emirates",
        "Vancouver, BC, Canada",
        "",
    ],
)
def test_non_us_locations_are_rejected(location):
    assert not is_us_location(location)


def test_indiana_is_not_confused_with_india():
    assert is_us_location("Indianapolis, IN")
    assert is_us_location("Indiana")
    assert not is_us_location("Bangalore, India")


def test_filter_us_locations_keeps_only_us_entries():
    mixed = ["London, UK", "SF", "Toronto, ON, Canada", "Austin, TX"]
    assert filter_us_locations(mixed) == ["SF", "Austin, TX"]


def test_filter_us_locations_handles_none():
    assert filter_us_locations(None) == []


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Software", "Software Engineering"),
        ("Software Engineering", "Software Engineering"),
        ("AI/ML/Data", "AI / ML / Data"),
        ("Data Science, AI & Machine Learning", "AI / ML / Data"),
        ("Quant", "Quant"),
        ("Quantitative Finance", "Quant"),
        (None, "Other"),
    ],
)
def test_category_variants_collapse(raw, expected):
    assert normalize_category(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Offers Sponsorship", "Yes"),
        ("Does Not Offer Sponsorship", "No"),
        ("U.S. Citizenship is Required", "US citizens only"),
        ("Other", "Unknown"),
        (None, "Unknown"),
        ("something new upstream", "Unknown"),
    ],
)
def test_sponsorship_mapping(raw, expected):
    assert normalize_sponsorship(raw) == expected


@pytest.mark.parametrize(
    "locations,expected",
    [
        (["Remote in USA"], "Remote"),
        (["Hybrid - Austin, TX"], "Hybrid"),
        (["Remote", "Hybrid - NYC"], "Hybrid"),  # hybrid wins over remote
        (["San Jose, CA"], "On-site"),
        ([], "Unknown"),
    ],
)
def test_work_mode_inference(locations, expected):
    assert infer_work_mode(locations) == expected
