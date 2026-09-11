"""Cost from tokens: the one place the pricing formula lives.

Cache writes are charged above the input rate (1.25x for the 5-minute tier,
2x for the 1-hour tier) and cache reads well below it (0.1x). Unknown models
fall back to ``default`` so nothing is silently costed at zero; the entry's
``confidence`` says how much to trust the figure.
"""
from . import config


class Pricer:
    def __init__(self, pricing: dict | None = None):
        pricing = pricing or config.load_pricing()
        self.models = pricing.get("models", {})
        self.default = pricing.get("default", {"input": 0.0, "output": 0.0, "confidence": "fallback"})
        self.families = pricing.get("families", {})
        cache = pricing.get("cache", {})
        self.w5 = cache.get("write_5m_multiplier", 1.25)
        self.w1h = cache.get("write_1h_multiplier", 2.0)
        self.read_mult = cache.get("read_multiplier", 0.1)
        self.seen = {}

    def entry(self, model):
        return self.models.get(model) or self.default

    def cost(self, model, inp, out, eph5, eph1h, cache_write, cache_read):
        entry = self.entry(model)
        self.seen[model] = entry.get("confidence", "fallback")
        pin = entry.get("input", 0.0) / 1_000_000.0
        pout = entry.get("output", 0.0) / 1_000_000.0
        # If the tier split isn't reported, treat all cache writes as 5m.
        if not eph5 and not eph1h and cache_write:
            eph5 = cache_write
        return (
            inp * pin
            + out * pout
            + eph5 * pin * self.w5
            + eph1h * pin * self.w1h
            + cache_read * pin * self.read_mult
        )

    def family_model(self, family):
        """The model id that stands for a family ('opus') in counterfactuals."""
        model = self.families.get(family)
        return model if model in self.models else None
