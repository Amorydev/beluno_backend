# ADR 0005: Security and Privacy

**Status:** Accepted

Authorization is server-side and deny-by-default. RLS is defense in depth.
Invite tokens are high-entropy and hashed. Booking codes, exports, credentials,
and signed URLs are restricted; plan locations, balances, receipts, and contacts
are sensitive. Media enters quarantine before it is accessible.
