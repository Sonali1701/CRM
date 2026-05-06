"""
CLI helper: python cli.py <command>

Commands:
  create-admin     Create the first admin user interactively
  seed             Seed demo data (leads, clients, deals, activities)
  db-upgrade       Run Alembic migrations (same as alembic upgrade head)
"""
import sys
from dotenv import load_dotenv

load_dotenv()


def create_admin():
    from app.database import SessionLocal
    from app.models import User
    from app.models.user import UserRole
    from app.security import hash_password

    email = input("Admin email: ").strip().lower()
    name = input("Full name: ").strip()
    password = input("Password (min 8 chars): ").strip()
    if len(password) < 8:
        print("Password too short."); return

    db = SessionLocal()
    if db.query(User).filter(User.email == email).first():
        print(f"User {email} already exists."); db.close(); return
    user = User(email=email, full_name=name, password_hash=hash_password(password), role=UserRole.ADMIN)
    db.add(user); db.commit()
    print(f"Admin user created: {email}")
    db.close()


def seed():
    from datetime import datetime, timezone, timedelta, date
    from app.database import SessionLocal
    from app.models import User, Lead, Client, Contact, Deal, Activity
    from app.models.user import UserRole
    from app.models.lead import LeadStatus
    from app.models.client import ClientType
    from app.models.deal import DealStage
    from app.models.activity import ActivityType
    from app.security import hash_password

    db = SessionLocal()

    admin = db.query(User).filter(User.role == UserRole.ADMIN).first()
    if not admin:
        admin = User(email="admin@radixsol.com", full_name="Admin User",
                     password_hash=hash_password("admin1234"), role=UserRole.ADMIN)
        db.add(admin); db.flush()

    sales1 = User(email="alice@radixsol.com", full_name="Alice Chen",
                  password_hash=hash_password("password1"), role=UserRole.SALES)
    sales2 = User(email="bob@radixsol.com", full_name="Bob Martinez",
                  password_hash=hash_password("password1"), role=UserRole.SALES)
    mgr = User(email="manager@radixsol.com", full_name="Sarah Manager",
               password_hash=hash_password("password1"), role=UserRole.MANAGER)
    db.add_all([sales1, sales2, mgr]); db.flush()

    # Leads
    leads_data = [
        ("David Kim", "NovaCare Health", "david@novacare.com", LeadStatus.NEW, sales1.id),
        ("Priya Patel", "MedStaff Pro", "priya@medstaff.com", LeadStatus.CONTACTED, sales1.id),
        ("Tom Wilson", "Allied Health", "tom@alliedhealth.com", LeadStatus.QUALIFIED, sales2.id),
        ("Lisa Park", "CareFirst MSP", "lisa@carefirst.com", LeadStatus.DISQUALIFIED, sales2.id),
    ]
    for name, company, email, status, owner_id in leads_data:
        db.add(Lead(name=name, company=company, email=email, status=status, owner_id=owner_id, source="LinkedIn"))
    db.flush()

    # Clients
    acme = Client(name="Acme Healthcare", type=ClientType.DIRECT, industry="Healthcare", owner_id=sales1.id)
    msp = Client(name="MedForce MSP", type=ClientType.MSP, industry="Staffing", owner_id=sales2.id)
    db.add_all([acme, msp]); db.flush()

    db.add(Contact(client_id=acme.id, full_name="Jennifer Hayes", title="Director of Nursing",
                   email="jennifer@acme.com", is_primary=True))
    db.add(Contact(client_id=acme.id, full_name="Mark Torres", title="HR Manager", email="mark@acme.com"))
    db.add(Contact(client_id=msp.id, full_name="Rachel Green", title="VP Procurement",
                   email="rachel@medforce.com", is_primary=True))
    db.flush()

    now = datetime.now(timezone.utc)
    deals_data = [
        ("Acme – ICU RN Placement", acme.id, sales1.id, 45000, DealStage.PROPOSAL_SHARED, 60,
         date.today() + timedelta(days=15)),
        ("Acme – ER Nurse Contract", acme.id, sales1.id, 32000, DealStage.NEGOTIATION, 80,
         date.today() + timedelta(days=8)),
        ("MedForce – Q3 Staffing Block", msp.id, sales2.id, 120000, DealStage.DISCOVERY_DONE, 30,
         date.today() + timedelta(days=30)),
        ("MedForce – Travel Nurse Program", msp.id, sales2.id, 78000, DealStage.QUALIFIED, 20,
         date.today() + timedelta(days=45)),
        ("Acme – Lab Tech Placement", acme.id, sales1.id, 18000, DealStage.LEAD_GENERATED, 10, None),
    ]
    deals = []
    for title, client_id, owner_id, value, stage, prob, close_date in deals_data:
        d = Deal(title=title, client_id=client_id, owner_id=owner_id, value=value,
                 stage=stage, probability=prob, expected_close_date=close_date,
                 last_activity_at=now - timedelta(days=2))
        db.add(d); deals.append(d)
    db.flush()

    # Make one deal stale
    deals[-1].last_activity_at = now - timedelta(days=10)

    db.add(Activity(type=ActivityType.CALL, subject="Discovery call with Jennifer",
                    body="Discussed ICU requirements, 3 RNs needed by month end.",
                    deal_id=deals[0].id, created_by_id=sales1.id,
                    due_at=now + timedelta(days=2), completed=False))
    db.add(Activity(type=ActivityType.EMAIL, subject="Sent proposal to Acme",
                    deal_id=deals[0].id, created_by_id=sales1.id, completed=True,
                    completed_at=now - timedelta(days=1)))
    db.add(Activity(type=ActivityType.MEETING, subject="Negotiation meeting",
                    deal_id=deals[1].id, created_by_id=sales1.id,
                    due_at=now - timedelta(hours=2), completed=False))  # overdue

    db.commit()
    db.close()
    print("Seed data inserted.")
    print("Users created:")
    print(f"  admin@radixsol.com / admin1234  (Admin)")
    print(f"  manager@radixsol.com / password1  (Manager)")
    print(f"  alice@radixsol.com / password1  (Sales)")
    print(f"  bob@radixsol.com / password1  (Sales)")


def db_upgrade():
    import subprocess, sys
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"])


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "create-admin":
        create_admin()
    elif cmd == "seed":
        seed()
    elif cmd == "db-upgrade":
        db_upgrade()
    else:
        print(__doc__)
