"""Tiny pet-shop backend used to demo Flag Doctor. It reads flags through the Flagsmith Python SDK."""

from flagsmith import Flagsmith

flagsmith = Flagsmith(environment_key="ser.demo_key")


def checkout(cart):
    flags = flagsmith.get_environment_flags()
    if flags.is_feature_enabled("new_checkout"):
        total = sum(item["price"] * item["qty"] for item in cart)
        return {"flow": "one-page", "total": round(total, 2)}
    else:
        total = 0
        for item in cart:
            total += item["price"] * item["qty"]
        return {"flow": "legacy-3-step", "total": total}


def home_banner():
    flags = flagsmith.get_environment_flags()
    if flags.is_feature_enabled("holiday_banner_2024"):
        return "Happy holidays! 20% off all pet beds"
    return None


def theme():
    flags = flagsmith.get_environment_flags()
    return "dark" if flags.is_feature_enabled("dark_mode") else "light"


def signup_screen():
    flags = flagsmith.get_environment_flags()
    # Typo on purpose: this flag does not exist in Flagsmith, so it is always off.
    if flags.is_feature_enabled("new_onbaording"):
        return "new-onboarding"
    return "classic-signup"
