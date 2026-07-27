"""Unit tests for _derive_effect_type (SpoolmanDB-Community -> Bambuddy's
effect_type enum mapping).

Covers both the existing branch logic and the review finding: nothing
validated the derived value against ALLOWED_EFFECT_TYPES before returning
it, so a future edit that drifts from the schema enum (typo, renamed value)
would silently write a value the spool form / rendering code has never
heard of.
"""

import backend.app.api.routes.inventory as inventory_module
from backend.app.api.routes.inventory import _derive_effect_type


class TestDeriveEffectTypeBranches:
    def test_longitudinal_multi_color_is_gradient(self):
        variant = {"hexes": ["ff0000", "00ff00"], "multi_color_direction": "longitudinal"}
        assert _derive_effect_type(variant) == "gradient"

    def test_two_hexes_non_longitudinal_is_dual_color(self):
        variant = {"hexes": ["ff0000", "00ff00"], "multi_color_direction": "segmented"}
        assert _derive_effect_type(variant) == "dual-color"

    def test_three_hexes_non_longitudinal_is_tri_color(self):
        variant = {"hexes": ["ff0000", "00ff00", "0000ff"], "multi_color_direction": "segmented"}
        assert _derive_effect_type(variant) == "tri-color"

    def test_four_plus_hexes_non_longitudinal_is_multicolor(self):
        variant = {"hexes": ["ff0000", "00ff00", "0000ff", "ffff00"], "multi_color_direction": "segmented"}
        assert _derive_effect_type(variant) == "multicolor"

    def test_glow_flag(self):
        assert _derive_effect_type({"glow": True}) == "glow"

    def test_sparkle_pattern(self):
        assert _derive_effect_type({"pattern": "sparkle"}) == "sparkle"

    def test_marble_pattern(self):
        assert _derive_effect_type({"pattern": "marble"}) == "marble"

    def test_translucent_flag(self):
        assert _derive_effect_type({"translucent": True}) == "translucent"

    def test_matte_finish(self):
        assert _derive_effect_type({"finish": "matte"}) == "matte"

    def test_no_signal_returns_none(self):
        assert _derive_effect_type({}) is None

    def test_multi_color_direction_needs_at_least_two_hexes(self):
        """A single hex with a direction set is not actually multi-color."""
        variant = {"hexes": ["ff0000"], "multi_color_direction": "longitudinal"}
        assert _derive_effect_type(variant) is None


class TestDeriveEffectTypeEnumSafetyNet:
    def test_falls_back_to_none_when_derived_value_not_in_allowed_set(self, monkeypatch):
        """If ALLOWED_EFFECT_TYPES and this function's literals were ever to
        drift apart, the mismatch must degrade to no effect_type - not write
        a value nothing else in the app recognizes."""
        monkeypatch.setattr(inventory_module, "ALLOWED_EFFECT_TYPES", frozenset())
        assert _derive_effect_type({"glow": True}) is None

    def test_still_returns_the_value_when_it_is_in_the_allowed_set(self, monkeypatch):
        monkeypatch.setattr(inventory_module, "ALLOWED_EFFECT_TYPES", frozenset({"glow"}))
        assert _derive_effect_type({"glow": True}) == "glow"
