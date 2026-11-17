"""Opt-in PostgreSQL regression, confined to a generated temporary schema."""
import os
import uuid
import pytest
from sqlalchemy import text, event
from sqlalchemy.exc import DataError
from app.db import make_engine
from app.db_migrations import ensure_postgres_text_capacity


@pytest.mark.skipif(not os.getenv('PLANHARNESS_TEST_POSTGRES_URL'),reason='requires explicit isolated PostgreSQL test URL')
def test_legacy_varchar_capacity_is_expanded_without_losing_rows():
    url=os.environ['PLANHARNESS_TEST_POSTGRES_URL']
    root=make_engine(url);schema='qa_capacity_'+uuid.uuid4().hex
    with root.begin() as c:c.execute(text(f'CREATE SCHEMA {schema}'))
    engine=make_engine(url)
    @event.listens_for(engine,'connect')
    def choose_schema(conn,_):
        with conn.cursor() as cur:cur.execute(f'SET search_path TO {schema}')
        conn.commit()
    try:
        with engine.begin() as c:
            c.execute(text('CREATE TABLE coach_task_executions (task_id VARCHAR(32), goal VARCHAR(32))'))
            c.execute(text("INSERT INTO coach_task_executions VALUES ('existing','preserve me')"))
            c.execute(text('CREATE TABLE coach_business_snapshots (revision VARCHAR(32))'))
        with pytest.raises(DataError),engine.begin() as c:
            c.execute(text('INSERT INTO coach_task_executions VALUES (:id,:goal)'),{'id':str(uuid.uuid4()),'goal':'长期要求'*200})
        statements=ensure_postgres_text_capacity(engine)
        assert len(statements)==3
        with engine.begin() as c:
            c.execute(text('INSERT INTO coach_task_executions VALUES (:id,:goal)'),{'id':str(uuid.uuid4()),'goal':'长期要求'*200})
            assert c.scalar(text('SELECT count(*) FROM coach_task_executions'))==2
            assert c.scalar(text("SELECT goal FROM coach_task_executions WHERE task_id='existing'"))=='preserve me'
        assert ensure_postgres_text_capacity(engine)==[]
    finally:
        engine.dispose()
        with root.begin() as c:c.execute(text(f'DROP SCHEMA {schema} CASCADE'))
        root.dispose()
