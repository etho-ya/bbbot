
import asyncio
import asyncpg
import os
from dotenv import load_dotenv

load_dotenv()

async def run_migration():
    database_url = os.getenv("DATABASE_URL")
    if not database_url:
        print("Error: DATABASE_URL not found in .env")
        return

    # Convert sqlalchemy URL to asyncpg standard if needed
    if database_url.startswith("postgresql+asyncpg://"):
        dsn = database_url.replace("postgresql+asyncpg://", "postgresql://")
    else:
        dsn = database_url

    print(f"Connecting to {dsn.split('@')[-1]}...")
    
    try:
        conn = await asyncpg.connect(dsn)
        
        columns = [
            ("re_status", "VARCHAR"),
            ("re_score", "FLOAT"),
            ("re_reason", "TEXT"),
            ("re_responded_at", "TIMESTAMP")
        ]
        
        for col_name, col_type in columns:
            print(f"Checking column: {col_name}...")
            # Check if column exists
            exists = await conn.fetchval(f"""
                SELECT EXISTS (
                    SELECT 1 
                    FROM information_schema.columns 
                    WHERE table_name='signals' AND column_name='{col_name}'
                );
            """)
            
            if not exists:
                print(f"Adding column: {col_name} ({col_type})...")
                await conn.execute(f"ALTER TABLE signals ADD COLUMN {col_name} {col_type};")
                print(f"Column {col_name} added.")
            else:
                print(f"Column {col_name} already exists.")
                
        await conn.close()
        print("Migration finished successfully.")
        
    except Exception as e:
        print(f"Migration failed: {e}")

if __name__ == "__main__":
    asyncio.run(run_migration())
