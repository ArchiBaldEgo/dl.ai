import os, psycopg2

conn = psycopg2.connect(
    host=os.environ.get("DB_HOST", "db"), port=os.environ.get("DB_PORT", "5432"),
    dbname=os.environ["DB_NAME"], user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
)
sql = open("/app/tmp_analysis.sql").read()
stmts = []
buf = []
for line in sql.splitlines():
    if line.lstrip().startswith("\\"):
        continue
    buf.append(line)
    if line.rstrip().endswith(";"):
        stmt = "\n".join(buf).strip()
        if stmt:
            stmts.append(stmt)
        buf = []
cur = conn.cursor()
with conn:
    for stmt in stmts:
        cur.execute(stmt)
        if cur.description:
            print([d[0] for d in cur.description])
            for row in cur.fetchall():
                print(row)
        print("-" * 30)