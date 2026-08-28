## Mouxtaris (Cyprus Outage Bot)
Try it out: t.me/mouxtarisbot

### Overview
Mouxtaris (neighborhood administrator) is a Telegram bot that alerts users when power or water interruptions affect their areas in Cyprus.

Users can subscribe to standardized areas at different administrative levels, such as neighborhood, area, municipality, or district. When the service detects a new interruption announcement, it resolves the locations mentioned in the announcement against the project's standardized area manifest and notifies users whose subscriptions cover the affected area.

Users can subscribe to multiple areas. Subscriptions are hierarchical:

- A user subscribed to a broader area receives alerts for interruptions affecting any area within it.
- A user subscribed to a smaller area receives alerts for interruptions specifically affecting that area, as well as interruptions announced for a broader area containing it.

### Design
The project codebase consists of:
- Dispatcher service
- Telegram bot command service
- Four scrapers for websites: EAC (Electricity Authority Cyprus) and EOA (District Local Government Organisation) Pafos, Limassol and Nicosia. 
- SQL Database schema

The scraper services POST announcements to the `ingest/eoa` and `ingest/eac` endpoints of the dispatcher, which resolves 
them against a standardized area manifest and noifies users subscribed to relevant areas. Two EOA scraper services pass announcements
through a local LLM to parse the location and interruption times.

### Deployment
The bot is deployed in production on Oracle Cloud.

### Sources and notice
This project accesses only information that the EAC and EOAs publish publicly on their own websites, and relays it via Telegram to make it easier to access. It accesses no private, restricted, or personal data, and does not circumvent any access restriction. The authorities' own pages remain the authoritative source; users should verify there. This project claims no ownership of the underlying information, is provided as-is with no guarantee of accuracy or timeliness, and is not affiliated with, endorsed by, or operated by the EAC, any EOA, or any government body. If you represent a data provider and have questions or concerns, please open an issue or contact the maintainers at mouxtarisbot@proton.me.
