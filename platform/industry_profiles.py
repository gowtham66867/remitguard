"""Industry profiles for RemitGuard's horizontal payment-reconciliation MVP."""

INDUSTRY_PROFILES = {
    "healthcare": {
        "label": "Healthcare billing",
        "document_label": "EOB / remittance advice",
        "deduction_categories": ["recoupment", "prior_overpayment", "contractual_adjustment"],
    },
    "insurance": {
        "label": "Insurance claims",
        "document_label": "Claim settlement / adjustment statement",
        "deduction_categories": ["claim_recovery", "reserve_withholding", "subrogation"],
    },
    "logistics": {
        "label": "Logistics and freight",
        "document_label": "Freight settlement / carrier invoice",
        "deduction_categories": ["short_payment", "accessorial_charge", "chargeback"],
    },
    "saas": {
        "label": "SaaS and subscriptions",
        "document_label": "Billing statement / payout report",
        "deduction_categories": ["credit", "refund", "proration", "reserve_hold"],
    },
    "marketplace": {
        "label": "Marketplaces",
        "document_label": "Seller settlement / payout statement",
        "deduction_categories": ["reserve_hold", "refund", "chargeback", "platform_fee"],
    },
    "manufacturing": {
        "label": "Manufacturing and suppliers",
        "document_label": "Supplier remittance / invoice settlement",
        "deduction_categories": ["short_payment", "supplier_chargeback", "quality_hold"],
    },
}


def get_profile(industry: str) -> dict:
    key = (industry or "healthcare").strip().lower()
    return INDUSTRY_PROFILES.get(key, INDUSTRY_PROFILES["healthcare"])
