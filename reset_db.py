from app.database import Base
from app.database_brands import engine  

from app import models

Base.metadata.create_all(bind=engine)
print("All tables created successfully in brands.db")
