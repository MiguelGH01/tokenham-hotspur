"""Re-export so `from clinic_catalog import ...` keeps working."""

from clinic.clinic_catalog import load_catalog, location_ids, location_name, specialty_ids

__all__ = ["load_catalog", "location_ids", "location_name", "specialty_ids"]
