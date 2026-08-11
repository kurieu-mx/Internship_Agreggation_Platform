import pytest

from normalize import (
    filter_us_locations,
    infer_work_mode,
    is_us_location,
    normalize_category,
    normalize_sponsorship,
    split_locations,
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


# -- multi-location fields ---------------------------------------------------
#
# ATS boards pack several places into one string. Treating the whole field as
# a single location is how "Chicago; New York" - two of the largest US tech
# markets - used to be classified non-US and dropped entirely.


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Chicago; New York", ["Chicago", "New York"]),
        ("Singapore / Hong Kong", ["Singapore", "Hong Kong"]),
        ("Dublin OR London", ["Dublin", "London"]),
        ("San Francisco, CA • New York, NY", ["San Francisco, CA", "New York, NY"]),
        ("New York, Seattle", ["New York", "Seattle"]),           # comma as a list
        ("San Francisco, CA", ["San Francisco, CA"]),             # comma as city+state
        ("Chicago, United States", ["Chicago, United States"]),   # comma as city+country
        ("Toronto, ON, Canada", ["Toronto, ON, Canada"]),         # ends in a country
        ("London - UK2", ["London"]),                             # office code stripped
        ("San Francisco - SF9", ["San Francisco"]),
        ("Chicago", ["Chicago"]),
        ("", []),
    ],
)
def test_location_fields_split_into_individual_places(raw, expected):
    assert split_locations(raw) == expected


def test_splitting_does_not_repeat_a_place():
    assert split_locations("New York; New York") == ["New York"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Chicago; New York", ["Chicago", "New York"]),
        ("Atlanta; New York", ["Atlanta", "New York"]),
        # A list mixing a Canadian city with two US ones keeps the two.
        ("Toronto, New York, San Francisco", ["New York", "San Francisco"]),
        ("Bellevue, Washington; Mountain View, California",
         ["Bellevue, Washington", "Mountain View, California"]),
        ("New York, London, or Paris", ["New York"]),
        ("US / Canada", ["US"]),
        # Nothing US in these at all.
        ("Dublin OR London", []),
        ("London, Paris, Hong Kong, Tokyo", []),
        ("Singapore / Hong Kong", []),
    ],
)
def test_filter_keeps_the_us_places_out_of_a_mixed_field(raw, expected):
    assert filter_us_locations([raw]) == expected


@pytest.mark.parametrize(
    "location",
    ["Chicago", "New York", "San Francisco", "Ann Arbor, MI", "Seattle", "Austin"],
)
def test_bare_us_cities_are_recognised(location):
    """ATS boards very often omit the state; rejecting these loses most of them."""
    assert is_us_location(location)


@pytest.mark.parametrize(
    "location",
    ["London", "Singapore", "Bengaluru", "Amsterdam", "Toronto", "Tel Aviv"],
)
def test_bare_foreign_cities_are_rejected(location):
    assert not is_us_location(location)


@pytest.mark.parametrize("location", ["In-Office", "Remote", "Multiple Locations", "Hybrid"])
def test_strings_that_name_no_country_are_rejected(location):
    """Precision over recall: an unplaceable string is not evidence of the US."""
    assert not is_us_location(location)


def test_explicitly_us_remote_is_still_accepted():
    assert is_us_location("Remote - US")
    assert is_us_location("US Remote")


def test_a_duplicate_place_across_two_fields_appears_once():
    assert filter_us_locations(["Chicago; New York", "New York, NY", "Chicago"]) == [
        "Chicago", "New York", "New York, NY",
    ]


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
