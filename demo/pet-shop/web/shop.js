// Pet-shop frontend used to demo Flag Doctor. Uses the Flagsmith JavaScript SDK.
import flagsmith from "flagsmith";

export async function initFlags() {
  await flagsmith.init({ environmentID: "demo_client_key" });
}

export function renderCheckoutButton(container) {
  const button = document.createElement("button");
  if (flagsmith.hasFeature("new_checkout")) {
    button.textContent = "Pay now";
    button.className = "btn btn-primary one-page";
  } else {
    button.textContent = "Continue to shipping (step 1 of 3)";
    button.className = "btn legacy";
  }
  container.appendChild(button);
}

// A/B test (Flagsmith Experimentation): which hero button text gets more "Get Directions" clicks?
const CTA_TEXT = {
  control: "📍 Visit Our Store",
  plan_visit: "🗓️ Plan Your Visit",
};

export function renderHeroButton(button) {
  const flag = flagsmith.getExperimentFlag("hero_cta_text"); // records the exposure
  const text = CTA_TEXT[flag && flag.value];
  if (text) button.textContent = text;
}

export function onGetDirections() {
  flagsmith.trackEvent("get_directions_click"); // conversion
}

export function renderPricing(container) {
  const layout = flagsmith.getValue("pricing_experiment") || "control";
  container.dataset.layout = layout;
}

export function renderBanner(container) {
  if (flagsmith.hasFeature("holiday_banner_2024")) {
    const banner = document.createElement("div");
    banner.className = "banner holiday";
    banner.textContent = "Happy holidays! 20% off all pet beds";
    container.prepend(banner);
  }
}
