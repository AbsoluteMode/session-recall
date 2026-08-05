"""Team hub — one shared index, fed by every member's raw transcripts.

The solo product is local-first by design: the index never leaves the machine.
A hub is the deliberate opposite, for a team that has agreed to pool its
history on a server it owns. Nothing here activates by accident — a client
without hub configuration behaves exactly as before.

The shape of the server is chosen to keep the two modes from diverging: a hub
stores the raw transcripts its clients send, in the same directory layout they
have locally, and then runs the ORDINARY indexer over them. There is no
server-side extractor, no second storage format, and no parallel search path
to keep in sync — `extract`, `index`, `retrieve` and `grep` are the same code
in both modes.
"""
