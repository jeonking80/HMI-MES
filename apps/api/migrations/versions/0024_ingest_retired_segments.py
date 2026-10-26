"""Append-only exact retired IDs, retaining all schema23 chunks unchanged.

SQL is frozen in this revision, independent of runtime application modules.
Only canonical lowercase hyphenated UUID text is encoded in UUID's 16-byte form;
all other spellings remain text so PostgreSQL UUID normalization cannot alias IDs.
"""

from alembic import op

revision = "0024_ingest_retired_segments"
down_revision = "0023_ingest_compaction"
branch_labels = None
depends_on = None

_PRUNE_SQL = """CREATE OR REPLACE FUNCTION public.prune_ingest_batches_hours(
    retention_hours integer DEFAULT 168,max_batches integer DEFAULT 200,
    max_candidates integer DEFAULT 400) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
    DECLARE
        tick timestamptz:=clock_timestamp(); st public.ingest_retention_state%ROWTYPE;
        b record; s public.source_streams%ROWTYPE; ev jsonb; valid boolean;
        chosen boolean:=false; budget_exhausted boolean:=false;
        scanned integer:=0; removed integer:=0; compacted integer:=0; key bigint;
        expired_candidate record; pending_gateways text[]:='{}'; pending_batches text[]:='{}';
        pending_count integer:=0; pending_group record; group_offset integer;
        leaf text; lo timestamptz; hi timestamptz; alo timestamptz; ahi timestamptz;
        tags text[]; epochs text[]; unknown_time boolean; adjacent record;
        range_start numeric; range_end numeric; amount numeric; accumulator text;
        first_at timestamptz; slo timestamptz; shi timestamptz;
        frontiers jsonb; latest_capture jsonb; receipt_record public.ingest_receipts%ROWTYPE;
    BEGIN
        IF retention_hours IS NULL OR retention_hours<3 OR max_batches IS NULL
            OR max_batches<1 OR max_batches>1000 OR max_candidates IS NULL
            OR max_candidates<1 OR max_candidates>2000 THEN
            RAISE EXCEPTION 'Invalid bounded retention parameters'; END IF;
        IF NOT pg_try_advisory_xact_lock_shared(843001) THEN
            RETURN jsonb_build_object('status','BUSY','deleted_count',0,
                'scanned_count',0,'compacted_count',0,'budget_exhausted',false);
        END IF;
        SELECT * INTO st FROM public.ingest_retention_state WHERE singleton
            FOR UPDATE SKIP LOCKED;
        IF NOT FOUND THEN
            RETURN jsonb_build_object('status','BUSY','deleted_count',0,
                'scanned_count',0,'compacted_count',0,'budget_exhausted',false);
        END IF;
        IF st.last_attempt_at>tick-interval '1 minute' THEN
            RETURN jsonb_build_object('status','THROTTLED','deleted_count',0,
                'scanned_count',0,'compacted_count',0,'budget_exhausted',false);
        END IF;
        UPDATE public.ingest_retention_state SET last_attempt_at=tick WHERE singleton;
        FOR b IN SELECT ib.* FROM public.ingest_batches ib
            WHERE ib.stored_at<tick-make_interval(hours=>retention_hours)
            AND (st.cursor_stored_at IS NULL OR
                ROW(ib.stored_at,ib.gateway_id,ib.batch_id)>
                ROW(st.cursor_stored_at,st.cursor_gateway_id,st.cursor_batch_id))
            ORDER BY ib.stored_at,ib.gateway_id,ib.batch_id LIMIT max_candidates
        LOOP
            IF clock_timestamp()-tick>=interval '500 milliseconds' THEN
                budget_exhausted:=true;
                EXIT;
            END IF;
            scanned:=scanned+1;
            UPDATE public.ingest_retention_state SET cursor_stored_at=b.stored_at,
                cursor_gateway_id=b.gateway_id,cursor_batch_id=b.batch_id WHERE singleton;
            BEGIN
            IF NOT chosen THEN
                key:=('x'||substr(encode(sha256(convert_to('gateway:'||b.gateway_id,
                    'UTF8')),'hex'),1,16))::bit(64)::bigint;
                IF NOT pg_try_advisory_xact_lock(key) THEN RAISE SQLSTATE 'P1001'; END IF;
                BEGIN
                    SELECT * INTO s FROM public.source_streams
                        WHERE source_id=b.source_id AND stream_id=b.stream_id;
                    IF NOT FOUND THEN RAISE SQLSTATE 'P1001'; END IF;
                    PERFORM 1 FROM public.lines WHERE id=s.line_id FOR UPDATE NOWAIT;
                    SELECT * INTO s FROM public.source_streams
                        WHERE source_id=b.source_id AND stream_id=b.stream_id FOR UPDATE NOWAIT;
                EXCEPTION WHEN lock_not_available THEN RAISE SQLSTATE 'P1001';
                END;
            END IF;
            IF b.gateway_id<>s.gateway_id OR b.source_id<>s.source_id
                OR b.stream_id<>s.stream_id OR b.capture_generation>s.capture_generation
                OR s.conflicted OR s.reconciliation_required THEN RAISE SQLSTATE 'P1001'; END IF;
            IF (jsonb_array_length(b.body->'events')=0 AND (
                EXISTS(SELECT 1 FROM public.stream_gaps
                WHERE source_id=s.source_id AND stream_id=s.stream_id)
                OR EXISTS(SELECT 1 FROM public.event_inbox WHERE source_id=s.source_id
                    AND stream_id=s.stream_id AND applied_at IS NULL)
                OR EXISTS(SELECT 1 FROM public.command_requests WHERE source_id=s.source_id
                    AND stream_id=s.stream_id AND (collision_latched OR status IN
                    ('REQUESTED','DELIVERED','ACCEPTED','OUTCOME_UNKNOWN')))
                )) OR jsonb_array_length(COALESCE(b.body->'gaps','[]'::jsonb))>0
            THEN RAISE SQLSTATE 'P1001'; END IF;
            valid:=true;
            FOR ev IN SELECT value FROM jsonb_array_elements(b.body->'events') LOOP
                IF NOT EXISTS(SELECT 1 FROM public.event_inbox i
                    JOIN public.production_executions x ON x.execution_id=i.execution_id
                    JOIN public.run_reconciliations r ON r.execution_id=x.execution_id
                    JOIN public.execution_totals t ON t.execution_id=x.execution_id
                    JOIN public.stream_progress p ON p.source_id=i.source_id
                        AND p.stream_id=i.stream_id
                    WHERE i.event_id=ev->>'event_id' AND i.body=ev
                    AND i.source_id=s.source_id AND i.stream_id=s.stream_id
                    AND i.seq=(ev->>'seq')::numeric AND i.kind=ev->>'kind'
                    AND i.execution_id=ev->>'execution_id' AND i.applied_at IS NOT NULL
                    AND i.reason_code IS NULL AND i.reason IS NULL
                    AND x.source_id=s.source_id AND x.stream_id=s.stream_id
                    AND r.status='SETTLED' AND r.final_summary->>'final'='true'
                    AND r.final_summary->>'state'='ENDED'
                    AND r.final_summary->>'execution_id'=x.execution_id
                    AND r.final_summary->>'source_id'=s.source_id
                    AND r.final_summary->>'stream_id'=s.stream_id
                    AND r.final_summary->>'recipe_version'=x.recipe_version
                    AND r.final_summary->>'counter_epoch'=x.counter_epoch
                    AND public.retention_parameters_match(r.final_summary,x.parameters,
                        x.recipe_version)
                    AND (r.final_summary->>'wip_quantity')::numeric=0
                    AND r.final_summary->>'ended_at' IS NOT NULL
                    AND (r.final_summary->>'good_quantity')::numeric=t.good_quantity
                    AND (r.final_summary->>'reject_quantity')::numeric=t.reject_quantity
                    AND (r.final_summary->>'scrap_quantity')::numeric=t.scrap_quantity
                    AND (r.final_summary->>'input_quantity')::numeric=
                        t.good_quantity+t.reject_quantity+t.scrap_quantity
                    AND p.verified_through_seq>=(r.final_summary->>'last_event_seq')::numeric
                    AND i.seq<=p.verified_through_seq
                    AND NOT EXISTS(SELECT 1 FROM public.stream_gaps g
                        WHERE g.source_id=s.source_id AND g.stream_id=s.stream_id
                        AND g.from_seq<=(r.final_summary->>'last_event_seq')::numeric)
                    AND NOT EXISTS(SELECT 1 FROM public.event_inbox q
                        WHERE q.source_id=s.source_id AND q.stream_id=s.stream_id
                        AND q.seq<=(r.final_summary->>'last_event_seq')::numeric
                        AND q.applied_at IS NULL)
                    AND NOT EXISTS(SELECT 1 FROM public.command_requests c
                        WHERE (c.execution_id=x.execution_id OR (c.execution_id IS NULL
                            AND c.source_id=s.source_id AND c.stream_id=s.stream_id))
                        AND (c.collision_latched OR
                        c.status IN ('REQUESTED','DELIVERED','ACCEPTED','OUTCOME_UNKNOWN')))
                    AND NOT EXISTS(SELECT 1 FROM public.event_inbox later
                        WHERE later.source_id=s.source_id AND later.stream_id=s.stream_id
                        AND later.execution_id=x.execution_id
                        AND later.seq>(r.final_summary->>'last_event_seq')::numeric
                        AND (later.applied_at IS NULL OR later.seq>p.verified_through_seq
                            OR later.kind<>'STATE_CHANGED'
                            OR COALESCE(later.body#>>'{payload,previous_state}','')
                                NOT IN ('STOPPED','RECOVERY_REQUIRED')
                            OR COALESCE(later.body#>>'{payload,state}','')
                                NOT IN ('STOPPED','RECOVERY_REQUIRED')))
                    AND COALESCE((SELECT last_event.body#>>'{payload,state}'
                        FROM public.event_inbox last_event
                        WHERE last_event.source_id=s.source_id
                        AND last_event.stream_id=s.stream_id
                        AND last_event.execution_id=x.execution_id
                        AND last_event.seq>(r.final_summary->>'last_event_seq')::numeric
                        ORDER BY last_event.seq DESC LIMIT 1),'STOPPED')='STOPPED'
                    AND NOT EXISTS(SELECT 1 FROM public.run_summary_conflicts c
                        WHERE c.execution_id=x.execution_id)) THEN valid:=false; EXIT; END IF;
            END LOOP;
            IF NOT valid THEN RAISE SQLSTATE 'P1001'; END IF;
            PERFORM 1 FROM public.ingest_batches WHERE gateway_id=b.gateway_id
                AND batch_id=b.batch_id FOR UPDATE SKIP LOCKED;
            IF NOT FOUND THEN RAISE SQLSTATE 'P1001'; END IF;
            chosen:=true;
            leaf:=encode(sha256(convert_to(jsonb_build_array('HMI_RETENTION_LEAF_V1',
                b.gateway_id,b.source_id,b.stream_id,b.capture_generation::text,
                b.capture_seq::text,b.batch_id,b.content_sha256)::text,'UTF8')),'hex');
            SELECT min(v),max(v) INTO lo,hi FROM (
                SELECT (b.body->>'captured_at')::timestamptz AS v
                UNION ALL SELECT (value->>'acquired_at')::timestamptz
                    FROM jsonb_array_elements(b.body->'samples')) times;
            SELECT c.status<>'GOOD',c.canonical_api_utc+(lo-c.original_capture_utc),
                c.canonical_api_utc+(hi-c.original_capture_utc)
                INTO unknown_time,alo,ahi FROM public.capture_clock_evidence c
                WHERE c.gateway_id=b.gateway_id AND c.batch_id=b.batch_id;
            unknown_time:=COALESCE(unknown_time,true) OR EXISTS(
                SELECT 1 FROM public.analog_history_clock_state cs
                JOIN public.ingest_receipts receipt ON receipt.gateway_id=b.gateway_id
                    AND receipt.batch_id=b.batch_id
                WHERE cs.gateway_id=b.gateway_id AND cs.source_id=b.source_id
                    AND cs.stream_id=b.stream_id
                    AND cs.capture_generation=b.capture_generation
                    AND cs.clock_epoch=receipt.clock_epoch
                    AND cs.reversal_seq IS NOT NULL AND b.capture_seq>=cs.reversal_seq);
            IF unknown_time THEN alo:=NULL; ahi:=NULL; END IF;
            SELECT COALESCE(array_agg(DISTINCT value->>'tag_id'),'{}'::text[]) INTO tags
                FROM jsonb_array_elements(b.body->'samples');
            epochs:=ARRAY(SELECT clock_epoch FROM public.ingest_receipts
                WHERE gateway_id=b.gateway_id AND batch_id=b.batch_id
                    AND clock_epoch IS NOT NULL);
            SELECT * INTO STRICT receipt_record FROM public.ingest_receipts
                WHERE gateway_id=b.gateway_id AND batch_id=b.batch_id;
            latest_capture:=to_jsonb(receipt_record)-'expired_at' || jsonb_build_object(
                'capture_generation',receipt_record.capture_generation::text,
                'capture_seq',receipt_record.capture_seq::text);
            frontiers:=CASE WHEN receipt_record.clock_epoch IS NULL THEN '{}'::jsonb ELSE
                jsonb_build_object(receipt_record.clock_epoch,jsonb_build_object('first',
                    jsonb_build_object('capture_seq',receipt_record.capture_seq::text,
                    'captured_at',receipt_record.captured_at,'monotonic_seconds',receipt_record.monotonic_seconds),
                    'last',jsonb_build_object('capture_seq',receipt_record.capture_seq::text,
                    'captured_at',receipt_record.captured_at,
                    'monotonic_seconds',receipt_record.monotonic_seconds))) END;
            range_start:=b.capture_seq; range_end:=b.capture_seq; amount:=1;
            accumulator:=leaf; first_at:=tick; slo:=b.stored_at; shi:=b.stored_at;
            FOR adjacent IN SELECT * FROM public.ingest_retention_ranges r
                WHERE r.gateway_id=b.gateway_id AND r.source_id=b.source_id
                AND r.stream_id=b.stream_id AND r.capture_generation=b.capture_generation
                AND (r.through_capture_seq=b.capture_seq-1
                    OR r.from_capture_seq=b.capture_seq+1) FOR UPDATE
            LOOP
                frontiers:=public.retention_clock_frontiers_merge(frontiers,adjacent.clock_frontiers);
                IF (adjacent.latest_capture->>'capture_seq')::numeric>
                    (latest_capture->>'capture_seq')::numeric THEN
                    latest_capture:=adjacent.latest_capture;
                END IF;
                range_start:=least(range_start,adjacent.from_capture_seq);
                range_end:=greatest(range_end,adjacent.through_capture_seq);
                amount:=amount+adjacent.deleted_count;
                accumulator:=public.retention_hash_add(accumulator,adjacent.hash_accumulator);
                first_at:=least(first_at,adjacent.first_deleted_at);
                slo:=least(slo,adjacent.stored_from); shi:=greatest(shi,adjacent.stored_until);
                lo:=least(lo,adjacent.original_from); hi:=greatest(hi,adjacent.original_until);
                alo:=least(alo,adjacent.analytical_from);
                ahi:=greatest(ahi,adjacent.analytical_until);
                unknown_time:=unknown_time OR adjacent.time_unknown;
                tags:=ARRAY(SELECT DISTINCT unnest(tags||adjacent.tag_ids) ORDER BY 1);
                epochs:=ARRAY(SELECT DISTINCT unnest(epochs||adjacent.clock_epochs) ORDER BY 1);
                DELETE FROM public.ingest_retention_ranges
                    WHERE gateway_id=adjacent.gateway_id AND source_id=adjacent.source_id
                    AND stream_id=adjacent.stream_id
                    AND capture_generation=adjacent.capture_generation
                    AND from_capture_seq=adjacent.from_capture_seq;
            END LOOP;
            INSERT INTO public.ingest_retention_ranges VALUES(s.line_id,b.gateway_id,
                b.source_id,b.stream_id,b.capture_generation,range_start,range_end,amount,
                'SHA256_SUM_MOD_2_256_V1',accumulator,slo,shi,lo,hi,alo,ahi,tags,epochs,
                unknown_time,first_at,tick,'RETENTION_EXPIRED',frontiers,latest_capture);
            UPDATE public.ingest_receipts SET expired_at=tick
                WHERE gateway_id=b.gateway_id AND batch_id=b.batch_id;
            DELETE FROM public.capture_clock_probes p WHERE p.gateway_id=b.gateway_id
                AND p.source_id=b.source_id AND p.stream_id=b.stream_id
                AND p.capture_generation=b.capture_generation AND p.consumed_batch_id=b.batch_id
                AND EXISTS(SELECT 1 FROM public.capture_clock_evidence c
                    WHERE c.gateway_id=b.gateway_id AND c.batch_id=b.batch_id
                    AND c.input_evidence_sha256=p.consumed_input_sha256);
            DELETE FROM public.capture_clock_evidence
                WHERE gateway_id=b.gateway_id AND batch_id=b.batch_id;
            DELETE FROM public.ingest_batches WHERE gateway_id=b.gateway_id AND batch_id=b.batch_id;
            removed:=removed+1;
            pending_gateways:=array_append(pending_gateways,b.gateway_id);
            pending_batches:=array_append(pending_batches,b.batch_id);
            pending_count:=pending_count+1;
            IF removed>=max_batches THEN EXIT; END IF;
            EXCEPTION WHEN SQLSTATE 'P1001' THEN NULL;
            END;
        END LOOP;
        IF NOT budget_exhausted AND scanned<max_candidates AND removed<max_batches THEN
            UPDATE public.ingest_retention_state SET cursor_stored_at=NULL,
                cursor_gateway_id=NULL,cursor_batch_id=NULL WHERE singleton;
        END IF;
        FOR expired_candidate IN SELECT gateway_id,batch_id,expired_at FROM public.ingest_receipts
            WHERE expired_at IS NOT NULL AND (st.compact_expired_at IS NULL OR
                ROW(expired_at,gateway_id,batch_id)>
                ROW(st.compact_expired_at,st.compact_gateway_id,st.compact_batch_id))
            ORDER BY expired_at,gateway_id,batch_id LIMIT max_candidates
        LOOP
            IF scanned>=max_candidates OR pending_count>=max_batches THEN EXIT; END IF;
            IF clock_timestamp()-tick>=interval '500 milliseconds' THEN
                budget_exhausted:=true; EXIT;
            END IF;
            scanned:=scanned+1;
            UPDATE public.ingest_retention_state
                SET compact_expired_at=expired_candidate.expired_at,
                compact_gateway_id=expired_candidate.gateway_id,
                compact_batch_id=expired_candidate.batch_id WHERE singleton;
            BEGIN
                key:=('x'||substr(encode(sha256(convert_to('gateway:'||expired_candidate.gateway_id,
                    'UTF8')),'hex'),1,16))::bit(64)::bigint;
                IF pg_try_advisory_xact_lock(key) AND NOT EXISTS(
                    SELECT 1 FROM unnest(pending_gateways,pending_batches) q(gateway,batch)
                    WHERE q.gateway=expired_candidate.gateway_id
                        AND q.batch=expired_candidate.batch_id) THEN
                    pending_gateways:=array_append(pending_gateways,expired_candidate.gateway_id);
                    pending_batches:=array_append(pending_batches,expired_candidate.batch_id);
                    pending_count:=pending_count+1;
                END IF;
            END;
        END LOOP;
        IF NOT budget_exhausted AND scanned<max_candidates AND pending_count<max_batches THEN
            UPDATE public.ingest_retention_state SET compact_expired_at=NULL,
                compact_gateway_id=NULL,compact_batch_id=NULL WHERE singleton;
        END IF;
        FOR pending_group IN SELECT gateway,array_agg(batch ORDER BY batch) AS batches
            FROM unnest(pending_gateways,pending_batches) q(gateway,batch)
            GROUP BY gateway ORDER BY gateway
        LOOP
            FOR group_offset IN 0..((cardinality(pending_group.batches)-1)/256) LOOP
                compacted:=compacted+public.compact_ingest_receipts(pending_group.gateway,
                    pending_group.batches[group_offset*256+1:(group_offset+1)*256],
                    tick+interval '500 milliseconds');
            END LOOP;
        END LOOP;
        IF clock_timestamp()-tick>=interval '500 milliseconds' THEN
            budget_exhausted:=true;
        END IF;
        RETURN jsonb_build_object('status',CASE WHEN removed>0 THEN 'PRUNED' ELSE 'EMPTY' END,
            'deleted_count',removed,'compacted_count',compacted,'scanned_count',scanned,
            'budget_exhausted',budget_exhausted);
    END $$"""

_RETENTION_GUARD_SQL = """CREATE OR REPLACE FUNCTION public.guard_ingest_retention() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public,pg_temp AS $$
        BEGIN
            IF current_user=(SELECT pg_get_userbyid(relowner) FROM pg_class
                WHERE oid='public.ingest_receipts'::regclass) AND TG_OP<>'TRUNCATE' THEN
                IF TG_TABLE_NAME='ingest_receipts' THEN
                    IF TG_OP='UPDATE' AND OLD.expired_at IS NULL AND NEW.expired_at IS NOT NULL
                        AND (to_jsonb(NEW)-'expired_at')=(to_jsonb(OLD)-'expired_at')
                        AND EXISTS(SELECT 1 FROM public.ingest_retention_ranges r
                            WHERE r.gateway_id=OLD.gateway_id AND r.source_id=OLD.source_id
                            AND r.stream_id=OLD.stream_id
                            AND r.capture_generation=OLD.capture_generation
                            AND OLD.capture_seq BETWEEN r.from_capture_seq
                                AND r.through_capture_seq)
                    THEN RETURN NEW; END IF;
                    IF TG_OP='DELETE' AND OLD.expired_at IS NOT NULL
                        AND public.retired_ingest_batch_exists(OLD.gateway_id,OLD.batch_id)
                        AND NOT EXISTS(SELECT 1 FROM public.ingest_batches b
                            WHERE b.gateway_id=OLD.gateway_id AND b.batch_id=OLD.batch_id)
                    THEN RETURN OLD; END IF;
                ELSIF TG_TABLE_NAME IN ('ingest_retention_ranges','ingest_retention_state',
                    'ingest_retired_batch_segments') THEN
                    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
                    RETURN NEW;
                END IF;
            END IF;
            RAISE EXCEPTION 'Retention identity and evidence are immutable';
        END $$"""

_IDENTITY_GUARD_SQL = """CREATE OR REPLACE FUNCTION
        public.guard_compacted_ingest_identity() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
        DECLARE key bigint;
        BEGIN
            key:=('x'||substr(encode(sha256(convert_to('gateway:'||NEW.gateway_id,
                'UTF8')),'hex'),1,16))::bit(64)::bigint;
            PERFORM pg_advisory_xact_lock(key);
            IF public.retired_ingest_batch_exists(NEW.gateway_id,NEW.batch_id)
                OR EXISTS(SELECT 1 FROM public.ingest_retention_ranges r
                    WHERE r.gateway_id=NEW.gateway_id
                    AND r.capture_generation=NEW.capture_generation
                    AND NEW.capture_seq BETWEEN r.from_capture_seq AND r.through_capture_seq)
            THEN RAISE EXCEPTION 'RETENTION_EXPIRED' USING ERRCODE='23514'; END IF;
            RETURN NEW;
        END $$"""


_BASE_SQL = (
    """CREATE TABLE public.ingest_retired_batch_segments (
        segment_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
        gateway_id text NOT NULL,
        uuid_ids uuid[] NOT NULL DEFAULT '{}'::uuid[],
        text_ids text[] NOT NULL DEFAULT '{}'::text[],
        CHECK(cardinality(uuid_ids)+cardinality(text_ids) BETWEEN 1 AND 256),
        CHECK(array_position(uuid_ids,NULL) IS NULL
            AND array_position(text_ids,NULL) IS NULL)
    )""",
    """CREATE INDEX ix_ingest_retired_segments_gateway
        ON public.ingest_retired_batch_segments(gateway_id)""",
    """CREATE INDEX ix_ingest_retired_segments_uuid
        ON public.ingest_retired_batch_segments USING gin(uuid_ids)""",
    """CREATE INDEX ix_ingest_retired_segments_text
        ON public.ingest_retired_batch_segments USING gin(text_ids)""",
    """CREATE FUNCTION public.retired_ingest_batch_exists(gateway text,batch text)
        RETURNS boolean LANGUAGE plpgsql STABLE STRICT SECURITY DEFINER
        SET search_path=pg_catalog,public,pg_temp AS $$
        BEGIN
            IF EXISTS(SELECT 1 FROM public.ingest_retired_batch_chunks c
                WHERE c.gateway_id=gateway AND c.batch_ids @> ARRAY[batch]) THEN
                RETURN true;
            END IF;
            IF length(batch)=36 AND batch COLLATE "C" ~
                '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
                RETURN EXISTS(SELECT 1 FROM public.ingest_retired_batch_segments s
                    WHERE s.gateway_id=gateway AND s.uuid_ids @> ARRAY[batch::uuid]);
            END IF;
            RETURN EXISTS(SELECT 1 FROM public.ingest_retired_batch_segments s
                WHERE s.gateway_id=gateway AND s.text_ids @> ARRAY[batch]);
        END $$""",
    """CREATE FUNCTION public.compact_ingest_receipts(gateway text,batches text[],
        deadline timestamptz DEFAULT NULL) RETURNS integer
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
        DECLARE p public.ingest_receipts%ROWTYPE; selected text[]:='{}';
            compact_uuids uuid[]:='{}'; compact_text text[]:='{}'; key bigint;
        BEGIN
            IF gateway IS NULL OR batches IS NULL OR cardinality(batches)>256
                OR array_position(batches,NULL) IS NOT NULL THEN
                RAISE EXCEPTION 'Invalid bounded compaction parameters';
            END IF;
            IF cardinality(batches)=0 THEN RETURN 0; END IF;
            key:=('x'||substr(encode(sha256(convert_to('gateway:'||gateway,
                'UTF8')),'hex'),1,16))::bit(64)::bigint;
            IF NOT pg_try_advisory_xact_lock(key) THEN RETURN 0; END IF;
            FOR p IN SELECT * FROM public.ingest_receipts
                WHERE gateway_id=gateway AND batch_id=ANY(batches) AND expired_at IS NOT NULL
                ORDER BY batch_id FOR UPDATE SKIP LOCKED
            LOOP
                IF deadline IS NOT NULL AND clock_timestamp()>=deadline THEN EXIT; END IF;
                IF EXISTS(SELECT 1 FROM public.ingest_batches
                    WHERE gateway_id=gateway AND batch_id=p.batch_id)
                    OR public.ingest_receipt_referenced(gateway,p.batch_id) THEN CONTINUE; END IF;
                IF NOT EXISTS(SELECT 1 FROM public.ingest_retention_ranges r
                    WHERE r.gateway_id=p.gateway_id AND r.source_id=p.source_id
                    AND r.stream_id=p.stream_id AND r.capture_generation=p.capture_generation
                    AND p.capture_seq BETWEEN r.from_capture_seq AND r.through_capture_seq
                    AND r.latest_capture IS NOT NULL
                    AND (p.clock_epoch IS NULL OR r.clock_frontiers ? p.clock_epoch)) THEN
                    RAISE EXCEPTION 'Compaction requires complete permanent clock metadata'; END IF;
                selected:=array_append(selected,p.batch_id);
                IF length(p.batch_id)=36 AND p.batch_id COLLATE "C" ~
                    '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$' THEN
                    compact_uuids:=array_append(compact_uuids,p.batch_id::uuid);
                ELSE
                    compact_text:=array_append(compact_text,p.batch_id);
                END IF;
            END LOOP;
            IF cardinality(selected)=0 THEN RETURN 0; END IF;
            INSERT INTO public.ingest_retired_batch_segments(gateway_id,uuid_ids,text_ids)
                VALUES(gateway,
                    ARRAY(SELECT u FROM unnest(compact_uuids) u ORDER BY u),
                    ARRAY(SELECT t FROM unnest(compact_text) t ORDER BY t COLLATE "C"));
            DELETE FROM public.ingest_receipts WHERE gateway_id=gateway AND batch_id=ANY(selected);
            RETURN cardinality(selected);
        END $$""",
    """CREATE OR REPLACE FUNCTION public.compact_ingest_receipt(gateway text,batch text)
        RETURNS boolean LANGUAGE sql SECURITY DEFINER
        SET search_path=pg_catalog,public,pg_temp AS $$
        SELECT public.compact_ingest_receipts(gateway,ARRAY[batch],NULL)>0
        $$""",
    """CREATE FUNCTION public.guard_retired_ingest_segments() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public,pg_temp AS $$
        BEGIN
            IF TG_TABLE_NAME='ingest_retired_batch_segments' AND TG_OP='INSERT'
                AND current_user=(SELECT pg_get_userbyid(relowner) FROM pg_class
                    WHERE oid='public.ingest_receipts'::regclass) THEN RETURN NEW; END IF;
            RAISE EXCEPTION 'Retired ingest identity segments are append-only';
        END $$""",
)


def upgrade_statements():
    statements = [*_BASE_SQL, _RETENTION_GUARD_SQL, _IDENTITY_GUARD_SQL, _PRUNE_SQL]
    # Legacy chunks remain queryable but can no longer be changed, even by the
    # owner through the old schema23 compaction entry point.
    for table in ("ingest_retired_batch_chunks", "ingest_retired_batch_segments"):
        for operation, scope in (
            ("INSERT OR UPDATE OR DELETE", "ROW"),
            ("TRUNCATE", "STATEMENT"),
        ):
            suffix = "append_only" if scope == "ROW" else "append_only_no_truncate"
            statements.append(f"""CREATE TRIGGER {table}_{suffix}
                BEFORE {operation} ON public.{table} FOR EACH {scope}
                EXECUTE FUNCTION public.guard_retired_ingest_segments()""")
    for function in (
        "retired_ingest_batch_exists(text,text)",
        "compact_ingest_receipts(text,text[],timestamptz)",
        "guard_retired_ingest_segments()",
    ):
        statements.append(f"REVOKE ALL ON FUNCTION public.{function} FROM PUBLIC")
    return tuple(statements)


def upgrade():
    for statement in upgrade_statements():
        op.execute(statement)


# Restore the frozen schema23 bodies verbatim, changing only CREATE to CREATE OR
# REPLACE. Existing legacy chunks and receipt/range clock evidence stay intact.
_DOWNGRADE_RESTORE_SQL = (
    """CREATE OR REPLACE FUNCTION
        public.compact_ingest_receipt(gateway text,batch text) RETURNS boolean
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
        DECLARE p public.ingest_receipts%ROWTYPE; bucket numeric;
        BEGIN
            SELECT * INTO p FROM public.ingest_receipts
                WHERE gateway_id=gateway AND batch_id=batch AND expired_at IS NOT NULL
                FOR UPDATE SKIP LOCKED;
            IF NOT FOUND OR EXISTS(SELECT 1 FROM public.ingest_batches
                WHERE gateway_id=gateway AND batch_id=batch)
                OR public.ingest_receipt_referenced(gateway,batch) THEN RETURN false; END IF;
            IF NOT EXISTS(SELECT 1 FROM public.ingest_retention_ranges r
                WHERE r.gateway_id=p.gateway_id AND r.source_id=p.source_id
                AND r.stream_id=p.stream_id AND r.capture_generation=p.capture_generation
                AND p.capture_seq BETWEEN r.from_capture_seq AND r.through_capture_seq
                AND r.latest_capture IS NOT NULL
                AND (p.clock_epoch IS NULL OR r.clock_frontiers ? p.clock_epoch)) THEN
                RAISE EXCEPTION 'Compaction requires complete permanent clock metadata'; END IF;
            bucket:=div(p.capture_seq-1,256);
            INSERT INTO public.ingest_retired_batch_chunks VALUES
                (p.gateway_id,p.capture_generation,bucket,ARRAY[p.batch_id])
                ON CONFLICT(gateway_id,capture_generation,seq_bucket) DO UPDATE
                SET batch_ids=ARRAY(SELECT DISTINCT unnest(
                    public.ingest_retired_batch_chunks.batch_ids||EXCLUDED.batch_ids) ORDER BY 1);
            DELETE FROM public.ingest_receipts WHERE gateway_id=gateway AND batch_id=batch;
            RETURN true;
        END $$""",
    """CREATE OR REPLACE FUNCTION public.guard_ingest_retention() RETURNS trigger
        LANGUAGE plpgsql SECURITY INVOKER SET search_path=pg_catalog,public,pg_temp AS $$
        BEGIN
            IF current_user=(SELECT pg_get_userbyid(relowner) FROM pg_class
                WHERE oid='public.ingest_receipts'::regclass) AND TG_OP<>'TRUNCATE' THEN
                IF TG_TABLE_NAME='ingest_receipts' THEN
                    IF TG_OP='UPDATE' AND OLD.expired_at IS NULL AND NEW.expired_at IS NOT NULL
                        AND (to_jsonb(NEW)-'expired_at')=(to_jsonb(OLD)-'expired_at')
                        AND EXISTS(SELECT 1 FROM public.ingest_retention_ranges r
                            WHERE r.gateway_id=OLD.gateway_id AND r.source_id=OLD.source_id
                            AND r.stream_id=OLD.stream_id
                            AND r.capture_generation=OLD.capture_generation
                            AND OLD.capture_seq BETWEEN r.from_capture_seq
                                AND r.through_capture_seq)
                    THEN RETURN NEW; END IF;
                    IF TG_OP='DELETE' AND OLD.expired_at IS NOT NULL AND EXISTS(
                        SELECT 1 FROM public.ingest_retired_batch_chunks c
                        WHERE c.gateway_id=OLD.gateway_id
                        AND c.capture_generation=OLD.capture_generation
                        AND c.seq_bucket=div(OLD.capture_seq-1,256)
                        AND c.batch_ids @> ARRAY[OLD.batch_id])
                        AND NOT EXISTS(SELECT 1 FROM public.ingest_batches b
                            WHERE b.gateway_id=OLD.gateway_id AND b.batch_id=OLD.batch_id)
                    THEN RETURN OLD; END IF;
                ELSIF TG_TABLE_NAME IN ('ingest_retention_ranges','ingest_retention_state',
                    'ingest_retired_batch_chunks') THEN
                    IF TG_OP='DELETE' THEN RETURN OLD; END IF;
                    RETURN NEW;
                END IF;
            END IF;
            RAISE EXCEPTION 'Retention identity and evidence are immutable';
        END $$""",
    """CREATE OR REPLACE FUNCTION
        public.guard_compacted_ingest_identity() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
        DECLARE key bigint;
        BEGIN
            key:=('x'||substr(encode(sha256(convert_to('gateway:'||NEW.gateway_id,
                'UTF8')),'hex'),1,16))::bit(64)::bigint;
            PERFORM pg_advisory_xact_lock(key);
            IF EXISTS(SELECT 1 FROM public.ingest_retired_batch_chunks c
                WHERE c.gateway_id=NEW.gateway_id AND c.batch_ids @> ARRAY[NEW.batch_id])
                OR EXISTS(SELECT 1 FROM public.ingest_retention_ranges r
                    WHERE r.gateway_id=NEW.gateway_id
                    AND r.capture_generation=NEW.capture_generation
                    AND NEW.capture_seq BETWEEN r.from_capture_seq AND r.through_capture_seq)
            THEN RAISE EXCEPTION 'RETENTION_EXPIRED' USING ERRCODE='23514'; END IF;
            RETURN NEW;
        END $$""",
    """CREATE OR REPLACE FUNCTION public.prune_ingest_batches_hours(
    retention_hours integer DEFAULT 168,max_batches integer DEFAULT 200,
    max_candidates integer DEFAULT 400) RETURNS jsonb
    LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public,pg_temp AS $$
    DECLARE
        tick timestamptz:=clock_timestamp(); st public.ingest_retention_state%ROWTYPE;
        b record; s public.source_streams%ROWTYPE; ev jsonb; valid boolean;
        chosen boolean:=false; budget_exhausted boolean:=false;
        scanned integer:=0; removed integer:=0; compacted integer:=0; key bigint;
        expired_candidate record;
        leaf text; lo timestamptz; hi timestamptz; alo timestamptz; ahi timestamptz;
        tags text[]; epochs text[]; unknown_time boolean; adjacent record;
        range_start numeric; range_end numeric; amount numeric; accumulator text;
        first_at timestamptz; slo timestamptz; shi timestamptz;
        frontiers jsonb; latest_capture jsonb; receipt_record public.ingest_receipts%ROWTYPE;
    BEGIN
        IF retention_hours IS NULL OR retention_hours<3 OR max_batches IS NULL
            OR max_batches<1 OR max_batches>1000 OR max_candidates IS NULL
            OR max_candidates<1 OR max_candidates>2000 THEN
            RAISE EXCEPTION 'Invalid bounded retention parameters'; END IF;
        IF NOT pg_try_advisory_xact_lock_shared(843001) THEN
            RETURN jsonb_build_object('status','BUSY','deleted_count',0,
                'scanned_count',0,'compacted_count',0,'budget_exhausted',false);
        END IF;
        SELECT * INTO st FROM public.ingest_retention_state WHERE singleton
            FOR UPDATE SKIP LOCKED;
        IF NOT FOUND THEN
            RETURN jsonb_build_object('status','BUSY','deleted_count',0,
                'scanned_count',0,'compacted_count',0,'budget_exhausted',false);
        END IF;
        IF st.last_attempt_at>tick-interval '1 minute' THEN
            RETURN jsonb_build_object('status','THROTTLED','deleted_count',0,
                'scanned_count',0,'compacted_count',0,'budget_exhausted',false);
        END IF;
        UPDATE public.ingest_retention_state SET last_attempt_at=tick WHERE singleton;
        FOR b IN SELECT ib.* FROM public.ingest_batches ib
            WHERE ib.stored_at<tick-make_interval(hours=>retention_hours)
            AND (st.cursor_stored_at IS NULL OR
                ROW(ib.stored_at,ib.gateway_id,ib.batch_id)>
                ROW(st.cursor_stored_at,st.cursor_gateway_id,st.cursor_batch_id))
            ORDER BY ib.stored_at,ib.gateway_id,ib.batch_id LIMIT max_candidates
        LOOP
            IF clock_timestamp()-tick>=interval '500 milliseconds' THEN
                budget_exhausted:=true;
                EXIT;
            END IF;
            scanned:=scanned+1;
            UPDATE public.ingest_retention_state SET cursor_stored_at=b.stored_at,
                cursor_gateway_id=b.gateway_id,cursor_batch_id=b.batch_id WHERE singleton;
            BEGIN
            IF NOT chosen THEN
                key:=('x'||substr(encode(sha256(convert_to('gateway:'||b.gateway_id,
                    'UTF8')),'hex'),1,16))::bit(64)::bigint;
                IF NOT pg_try_advisory_xact_lock(key) THEN RAISE SQLSTATE 'P1001'; END IF;
                BEGIN
                    SELECT * INTO s FROM public.source_streams
                        WHERE source_id=b.source_id AND stream_id=b.stream_id;
                    IF NOT FOUND THEN RAISE SQLSTATE 'P1001'; END IF;
                    PERFORM 1 FROM public.lines WHERE id=s.line_id FOR UPDATE NOWAIT;
                    SELECT * INTO s FROM public.source_streams
                        WHERE source_id=b.source_id AND stream_id=b.stream_id FOR UPDATE NOWAIT;
                EXCEPTION WHEN lock_not_available THEN RAISE SQLSTATE 'P1001';
                END;
            END IF;
            IF b.gateway_id<>s.gateway_id OR b.source_id<>s.source_id
                OR b.stream_id<>s.stream_id OR b.capture_generation>s.capture_generation
                OR s.conflicted OR s.reconciliation_required THEN RAISE SQLSTATE 'P1001'; END IF;
            IF (jsonb_array_length(b.body->'events')=0 AND (
                EXISTS(SELECT 1 FROM public.stream_gaps
                WHERE source_id=s.source_id AND stream_id=s.stream_id)
                OR EXISTS(SELECT 1 FROM public.event_inbox WHERE source_id=s.source_id
                    AND stream_id=s.stream_id AND applied_at IS NULL)
                OR EXISTS(SELECT 1 FROM public.command_requests WHERE source_id=s.source_id
                    AND stream_id=s.stream_id AND (collision_latched OR status IN
                    ('REQUESTED','DELIVERED','ACCEPTED','OUTCOME_UNKNOWN')))
                )) OR jsonb_array_length(COALESCE(b.body->'gaps','[]'::jsonb))>0
            THEN RAISE SQLSTATE 'P1001'; END IF;
            valid:=true;
            FOR ev IN SELECT value FROM jsonb_array_elements(b.body->'events') LOOP
                IF NOT EXISTS(SELECT 1 FROM public.event_inbox i
                    JOIN public.production_executions x ON x.execution_id=i.execution_id
                    JOIN public.run_reconciliations r ON r.execution_id=x.execution_id
                    JOIN public.execution_totals t ON t.execution_id=x.execution_id
                    JOIN public.stream_progress p ON p.source_id=i.source_id
                        AND p.stream_id=i.stream_id
                    WHERE i.event_id=ev->>'event_id' AND i.body=ev
                    AND i.source_id=s.source_id AND i.stream_id=s.stream_id
                    AND i.seq=(ev->>'seq')::numeric AND i.kind=ev->>'kind'
                    AND i.execution_id=ev->>'execution_id' AND i.applied_at IS NOT NULL
                    AND i.reason_code IS NULL AND i.reason IS NULL
                    AND x.source_id=s.source_id AND x.stream_id=s.stream_id
                    AND r.status='SETTLED' AND r.final_summary->>'final'='true'
                    AND r.final_summary->>'state'='ENDED'
                    AND r.final_summary->>'execution_id'=x.execution_id
                    AND r.final_summary->>'source_id'=s.source_id
                    AND r.final_summary->>'stream_id'=s.stream_id
                    AND r.final_summary->>'recipe_version'=x.recipe_version
                    AND r.final_summary->>'counter_epoch'=x.counter_epoch
                    AND public.retention_parameters_match(r.final_summary,x.parameters,
                        x.recipe_version)
                    AND (r.final_summary->>'wip_quantity')::numeric=0
                    AND r.final_summary->>'ended_at' IS NOT NULL
                    AND (r.final_summary->>'good_quantity')::numeric=t.good_quantity
                    AND (r.final_summary->>'reject_quantity')::numeric=t.reject_quantity
                    AND (r.final_summary->>'scrap_quantity')::numeric=t.scrap_quantity
                    AND (r.final_summary->>'input_quantity')::numeric=
                        t.good_quantity+t.reject_quantity+t.scrap_quantity
                    AND p.verified_through_seq>=(r.final_summary->>'last_event_seq')::numeric
                    AND i.seq<=p.verified_through_seq
                    AND NOT EXISTS(SELECT 1 FROM public.stream_gaps g
                        WHERE g.source_id=s.source_id AND g.stream_id=s.stream_id
                        AND g.from_seq<=(r.final_summary->>'last_event_seq')::numeric)
                    AND NOT EXISTS(SELECT 1 FROM public.event_inbox q
                        WHERE q.source_id=s.source_id AND q.stream_id=s.stream_id
                        AND q.seq<=(r.final_summary->>'last_event_seq')::numeric
                        AND q.applied_at IS NULL)
                    AND NOT EXISTS(SELECT 1 FROM public.command_requests c
                        WHERE (c.execution_id=x.execution_id OR (c.execution_id IS NULL
                            AND c.source_id=s.source_id AND c.stream_id=s.stream_id))
                        AND (c.collision_latched OR
                        c.status IN ('REQUESTED','DELIVERED','ACCEPTED','OUTCOME_UNKNOWN')))
                    AND NOT EXISTS(SELECT 1 FROM public.event_inbox later
                        WHERE later.source_id=s.source_id AND later.stream_id=s.stream_id
                        AND later.execution_id=x.execution_id
                        AND later.seq>(r.final_summary->>'last_event_seq')::numeric
                        AND (later.applied_at IS NULL OR later.seq>p.verified_through_seq
                            OR later.kind<>'STATE_CHANGED'
                            OR COALESCE(later.body#>>'{payload,previous_state}','')
                                NOT IN ('STOPPED','RECOVERY_REQUIRED')
                            OR COALESCE(later.body#>>'{payload,state}','')
                                NOT IN ('STOPPED','RECOVERY_REQUIRED')))
                    AND COALESCE((SELECT last_event.body#>>'{payload,state}'
                        FROM public.event_inbox last_event
                        WHERE last_event.source_id=s.source_id
                        AND last_event.stream_id=s.stream_id
                        AND last_event.execution_id=x.execution_id
                        AND last_event.seq>(r.final_summary->>'last_event_seq')::numeric
                        ORDER BY last_event.seq DESC LIMIT 1),'STOPPED')='STOPPED'
                    AND NOT EXISTS(SELECT 1 FROM public.run_summary_conflicts c
                        WHERE c.execution_id=x.execution_id)) THEN valid:=false; EXIT; END IF;
            END LOOP;
            IF NOT valid THEN RAISE SQLSTATE 'P1001'; END IF;
            PERFORM 1 FROM public.ingest_batches WHERE gateway_id=b.gateway_id
                AND batch_id=b.batch_id FOR UPDATE SKIP LOCKED;
            IF NOT FOUND THEN RAISE SQLSTATE 'P1001'; END IF;
            chosen:=true;
            leaf:=encode(sha256(convert_to(jsonb_build_array('HMI_RETENTION_LEAF_V1',
                b.gateway_id,b.source_id,b.stream_id,b.capture_generation::text,
                b.capture_seq::text,b.batch_id,b.content_sha256)::text,'UTF8')),'hex');
            SELECT min(v),max(v) INTO lo,hi FROM (
                SELECT (b.body->>'captured_at')::timestamptz AS v
                UNION ALL SELECT (value->>'acquired_at')::timestamptz
                    FROM jsonb_array_elements(b.body->'samples')) times;
            SELECT c.status<>'GOOD',c.canonical_api_utc+(lo-c.original_capture_utc),
                c.canonical_api_utc+(hi-c.original_capture_utc)
                INTO unknown_time,alo,ahi FROM public.capture_clock_evidence c
                WHERE c.gateway_id=b.gateway_id AND c.batch_id=b.batch_id;
            unknown_time:=COALESCE(unknown_time,true) OR EXISTS(
                SELECT 1 FROM public.analog_history_clock_state cs
                JOIN public.ingest_receipts receipt ON receipt.gateway_id=b.gateway_id
                    AND receipt.batch_id=b.batch_id
                WHERE cs.gateway_id=b.gateway_id AND cs.source_id=b.source_id
                    AND cs.stream_id=b.stream_id
                    AND cs.capture_generation=b.capture_generation
                    AND cs.clock_epoch=receipt.clock_epoch
                    AND cs.reversal_seq IS NOT NULL AND b.capture_seq>=cs.reversal_seq);
            IF unknown_time THEN alo:=NULL; ahi:=NULL; END IF;
            SELECT COALESCE(array_agg(DISTINCT value->>'tag_id'),'{}'::text[]) INTO tags
                FROM jsonb_array_elements(b.body->'samples');
            epochs:=ARRAY(SELECT clock_epoch FROM public.ingest_receipts
                WHERE gateway_id=b.gateway_id AND batch_id=b.batch_id
                    AND clock_epoch IS NOT NULL);
            SELECT * INTO STRICT receipt_record FROM public.ingest_receipts
                WHERE gateway_id=b.gateway_id AND batch_id=b.batch_id;
            latest_capture:=to_jsonb(receipt_record)-'expired_at' || jsonb_build_object(
                'capture_generation',receipt_record.capture_generation::text,
                'capture_seq',receipt_record.capture_seq::text);
            frontiers:=CASE WHEN receipt_record.clock_epoch IS NULL THEN '{}'::jsonb ELSE
                jsonb_build_object(receipt_record.clock_epoch,jsonb_build_object('first',
                    jsonb_build_object('capture_seq',receipt_record.capture_seq::text,
                    'captured_at',receipt_record.captured_at,'monotonic_seconds',receipt_record.monotonic_seconds),
                    'last',jsonb_build_object('capture_seq',receipt_record.capture_seq::text,
                    'captured_at',receipt_record.captured_at,
                    'monotonic_seconds',receipt_record.monotonic_seconds))) END;
            range_start:=b.capture_seq; range_end:=b.capture_seq; amount:=1;
            accumulator:=leaf; first_at:=tick; slo:=b.stored_at; shi:=b.stored_at;
            FOR adjacent IN SELECT * FROM public.ingest_retention_ranges r
                WHERE r.gateway_id=b.gateway_id AND r.source_id=b.source_id
                AND r.stream_id=b.stream_id AND r.capture_generation=b.capture_generation
                AND (r.through_capture_seq=b.capture_seq-1
                    OR r.from_capture_seq=b.capture_seq+1) FOR UPDATE
            LOOP
                frontiers:=public.retention_clock_frontiers_merge(frontiers,adjacent.clock_frontiers);
                IF (adjacent.latest_capture->>'capture_seq')::numeric>
                    (latest_capture->>'capture_seq')::numeric THEN
                    latest_capture:=adjacent.latest_capture;
                END IF;
                range_start:=least(range_start,adjacent.from_capture_seq);
                range_end:=greatest(range_end,adjacent.through_capture_seq);
                amount:=amount+adjacent.deleted_count;
                accumulator:=public.retention_hash_add(accumulator,adjacent.hash_accumulator);
                first_at:=least(first_at,adjacent.first_deleted_at);
                slo:=least(slo,adjacent.stored_from); shi:=greatest(shi,adjacent.stored_until);
                lo:=least(lo,adjacent.original_from); hi:=greatest(hi,adjacent.original_until);
                alo:=least(alo,adjacent.analytical_from);
                ahi:=greatest(ahi,adjacent.analytical_until);
                unknown_time:=unknown_time OR adjacent.time_unknown;
                tags:=ARRAY(SELECT DISTINCT unnest(tags||adjacent.tag_ids) ORDER BY 1);
                epochs:=ARRAY(SELECT DISTINCT unnest(epochs||adjacent.clock_epochs) ORDER BY 1);
                DELETE FROM public.ingest_retention_ranges
                    WHERE gateway_id=adjacent.gateway_id AND source_id=adjacent.source_id
                    AND stream_id=adjacent.stream_id
                    AND capture_generation=adjacent.capture_generation
                    AND from_capture_seq=adjacent.from_capture_seq;
            END LOOP;
            INSERT INTO public.ingest_retention_ranges VALUES(s.line_id,b.gateway_id,
                b.source_id,b.stream_id,b.capture_generation,range_start,range_end,amount,
                'SHA256_SUM_MOD_2_256_V1',accumulator,slo,shi,lo,hi,alo,ahi,tags,epochs,
                unknown_time,first_at,tick,'RETENTION_EXPIRED',frontiers,latest_capture);
            UPDATE public.ingest_receipts SET expired_at=tick
                WHERE gateway_id=b.gateway_id AND batch_id=b.batch_id;
            DELETE FROM public.capture_clock_probes p WHERE p.gateway_id=b.gateway_id
                AND p.source_id=b.source_id AND p.stream_id=b.stream_id
                AND p.capture_generation=b.capture_generation AND p.consumed_batch_id=b.batch_id
                AND EXISTS(SELECT 1 FROM public.capture_clock_evidence c
                    WHERE c.gateway_id=b.gateway_id AND c.batch_id=b.batch_id
                    AND c.input_evidence_sha256=p.consumed_input_sha256);
            DELETE FROM public.capture_clock_evidence
                WHERE gateway_id=b.gateway_id AND batch_id=b.batch_id;
            DELETE FROM public.ingest_batches WHERE gateway_id=b.gateway_id AND batch_id=b.batch_id;
            removed:=removed+1;
            IF public.compact_ingest_receipt(b.gateway_id,b.batch_id) THEN
                compacted:=compacted+1;
            END IF;
            IF removed>=max_batches THEN EXIT; END IF;
            EXCEPTION WHEN SQLSTATE 'P1001' THEN NULL;
            END;
        END LOOP;
        IF NOT budget_exhausted AND scanned<max_candidates AND removed<max_batches THEN
            UPDATE public.ingest_retention_state SET cursor_stored_at=NULL,
                cursor_gateway_id=NULL,cursor_batch_id=NULL WHERE singleton;
        END IF;
        FOR expired_candidate IN SELECT gateway_id,batch_id,expired_at FROM public.ingest_receipts
            WHERE expired_at IS NOT NULL AND (st.compact_expired_at IS NULL OR
                ROW(expired_at,gateway_id,batch_id)>
                ROW(st.compact_expired_at,st.compact_gateway_id,st.compact_batch_id))
            ORDER BY expired_at,gateway_id,batch_id LIMIT max_candidates
        LOOP
            IF scanned>=max_candidates OR compacted>=max_batches THEN EXIT; END IF;
            IF clock_timestamp()-tick>=interval '500 milliseconds' THEN
                budget_exhausted:=true; EXIT;
            END IF;
            scanned:=scanned+1;
            UPDATE public.ingest_retention_state
                SET compact_expired_at=expired_candidate.expired_at,
                compact_gateway_id=expired_candidate.gateway_id,
                compact_batch_id=expired_candidate.batch_id WHERE singleton;
            BEGIN
                key:=('x'||substr(encode(sha256(convert_to('gateway:'||expired_candidate.gateway_id,
                    'UTF8')),'hex'),1,16))::bit(64)::bigint;
                IF pg_try_advisory_xact_lock(key) AND
                    public.compact_ingest_receipt(expired_candidate.gateway_id,
                        expired_candidate.batch_id) THEN
                    compacted:=compacted+1;
                END IF;
            END;
        END LOOP;
        IF NOT budget_exhausted AND scanned<max_candidates AND compacted<max_batches THEN
            UPDATE public.ingest_retention_state SET compact_expired_at=NULL,
                compact_gateway_id=NULL,compact_batch_id=NULL WHERE singleton;
        END IF;
        RETURN jsonb_build_object('status',CASE WHEN removed>0 THEN 'PRUNED' ELSE 'EMPTY' END,
            'deleted_count',removed,'compacted_count',compacted,'scanned_count',scanned,
            'budget_exhausted',budget_exhausted);
    END $$""",
)


def downgrade():
    # Runtime maintenance takes the shared form of this schema lock. Check the
    # table while holding it exclusively so a segment cannot appear after the
    # emptiness check. Never erase a permanent identity during downgrade.
    op.execute("SELECT pg_catalog.pg_advisory_xact_lock(843001)")
    op.execute("LOCK TABLE public.ingest_retired_batch_segments IN ACCESS EXCLUSIVE MODE")
    op.execute("""DO $$ BEGIN
        IF EXISTS(SELECT 1 FROM public.ingest_retired_batch_segments) THEN
            RAISE EXCEPTION
                'Nonempty retired identity segments require a data-preserving forward migration';
        END IF;
    END $$""")
    for table in ("ingest_retired_batch_chunks", "ingest_retired_batch_segments"):
        for suffix in ("append_only", "append_only_no_truncate"):
            op.execute(f"DROP TRIGGER {table}_{suffix} ON public.{table} RESTRICT")
    for statement in _DOWNGRADE_RESTORE_SQL:
        op.execute(statement)
    for function in (
        "compact_ingest_receipts(text,text[],timestamptz)",
        "retired_ingest_batch_exists(text,text)",
        "guard_retired_ingest_segments()",
    ):
        op.execute(f"DROP FUNCTION public.{function} RESTRICT")
    op.execute("DROP TABLE public.ingest_retired_batch_segments RESTRICT")
