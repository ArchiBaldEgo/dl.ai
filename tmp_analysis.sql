\pset pager off
select r.id, r.run_id, r.created_at, r.model_key, r.verdict, left(r.dl_comment,42) as comment,
       left(replace(r.code, chr(10), '|'), 55) as code, left(r.raw_response, 45) as raw,
       (r.raw_response like '%```%') as fenced
from ai_ai_model_test_result r
where r.id between 5367 and 5380 order by r.id;
select id, run_id, status, started_at, finished_at, message from ai_ai_model_test_run order by id desc limit 4;