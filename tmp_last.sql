\pset pager off
select r.id, r.created_at, r.model_key, r.verdict, left(r.dl_comment,50) as comment, r.file_extension_snapshot as ext
from ai_ai_model_test_result r
order by r.id desc limit 5;