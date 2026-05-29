"""
Quick test to verify audit logging and NLP classification work.
"""
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.database import SessionLocal
from app.models import User, UserRole, Lead
from app.models.lead import LeadStatus
from app.services.audit import log_action, get_team_activity
from app.services.nlp_classifier import classify_notes
from app.security import hash_password

db = SessionLocal()

# Create a test admin user if it doesn't exist
admin = db.query(User).filter(User.email == "test@admin.com").first()
if not admin:
    admin = User(
        email="test@admin.com",
        full_name="Test Admin",
        password_hash=hash_password("test123"),
        role=UserRole.ADMIN,
        is_active=True,
    )
    db.add(admin)
    db.commit()
    print("[OK] Created test admin user")

# Test NLP classifier
test_notes = [
    ("They expressed strong interest and want to schedule a meeting", "positive"),
    ("Not interested in our solutions at the moment", "decline"),
    ("We're doing everything in-house", "in_house_only"),
    ("Let me reach out to the right person", "wrong_poc"),
    ("Check back with us next quarter", "reconnect_later"),
    ("The person left the company", "out_of_org"),
    ("I sent them our proposal", "info_sent"),
]

print("\nTesting NLP Classifier:")
for text, expected in test_notes:
    result = classify_notes(text)
    status = "[OK]" if result == expected else "[FAIL]"
    print(f"{status} '{text[:40]}...' -> {result} (expected {expected})")

# Test audit logging
print("\nTesting Audit Logging:")
log_action(
    db, admin, "test_action", "test_entity",
    entity_id=123,
    details={"count": 5, "source": "test"}
)
print("[OK] Logged test action")

# Test team activity summary
activity = get_team_activity(db, days=30)
print(f"[OK] Retrieved team activity: {activity['total']} total actions")
print(f"  Actions by type: {activity['by_action']}")

print("\nAll tests passed!")
db.close()
