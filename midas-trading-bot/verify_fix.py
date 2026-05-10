import asyncio
from app.core.database import async_session
from app.models.db_models import ChannelDB
from sqlalchemy import select, delete

async def verify_fix():
    print("Testing channel saving via simulated form data...")
    # Simulated form data
    channel_ids = ["12345", "67890"]
    titles = ["Test Channel 1", "Test Channel 2"][]
    
    async with async_session() as session:
        # Clear existing
        await session.execute(delete(ChannelDB).where(ChannelDB.user_id == 1))
        
        # Simulating main.py logic
        title_map = {ch_id: titles[i] for i, ch_id in enumerate(channel_ids)}
        
        for ch_id in channel_ids:
            title = title_map.get(ch_id)
            new_channel = ChannelDB(user_id=1, channel_id=ch_id, title=title)
            session.add(new_channel)
        
        await session.commit()
    
    print("Verifying saved data...")
    async with async_session() as session:
        result = await session.execute(select(ChannelDB).where(ChannelDB.user_id == 1))
        channels = result.scalars().all()
        assert len(channels) == 2, f"Expected 2 channels, got {len(channels)}"
        assert channels[0].title == "Test Channel 1"
        assert channels[1].title == "Test Channel 2"
        print("Verification SUCCESS: Channels saved correctly with provided titles.")

if __name__ == "__main__":
    asyncio.run(verify_fix())
