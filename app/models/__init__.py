"""Models package — imports all models so Alembic and Base.metadata see them."""

from app.models.appointment import Appointment
from app.models.base import Base
from app.models.consent import Consent
from app.models.consult import AgentRun, Consult, Prescription, SOAPNote
from app.models.doctor import DoctorProfile
from app.models.file import File
from app.models.patient import PatientProfile
from app.models.registry import RegistryRecord, VerificationCheck, VerificationEvent
from app.models.report import AuditLog, Report
from app.models.user import User

__all__ = [
    "Base",
    "User",
    "DoctorProfile",
    "PatientProfile",
    "File",
    "RegistryRecord",
    "VerificationCheck",
    "VerificationEvent",
    "Appointment",
    "Consent",
    "Consult",
    "SOAPNote",
    "Prescription",
    "AgentRun",
    "Report",
    "AuditLog",
]
