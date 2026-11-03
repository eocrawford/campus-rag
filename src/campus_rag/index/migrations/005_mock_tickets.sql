-- Mock ticketing: tickets filed through the loopback ticketing API (campus_rag.ticketing), which
-- stands in for an ITSM system such as ServiceNow.
--
-- request_id links a ticket to the request_log row of the answer that prompted it; the link is
-- cleared, not the ticket, when that log row is deleted. Tickets hold only what the person typed
-- and no user identity.

CREATE TABLE mock_tickets (
    id bigserial PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now(),
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    category text NOT NULL,
    summary text NOT NULL CHECK (char_length(summary) BETWEEN 5 AND 200),
    details text NOT NULL CHECK (char_length(details) <= 4000),
    source_question text,
    request_id bigint REFERENCES request_log (id) ON DELETE SET NULL
);

CREATE INDEX mock_tickets_status_created_at_idx ON mock_tickets (status, created_at);
