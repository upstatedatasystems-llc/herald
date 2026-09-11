
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from herald.core.models import HeraldRequest
from herald.core.pipeline import execute_script_generation, process_herald_request
from herald.db.models import Base, JobState, PodcastJob


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def test_case_a_unique_no_approval(db_session):
    """Case A: Unique content, hold_for_approval=False -> QUEUED_TTS directly."""
    req = HeraldRequest(
        source_text="This is a unique test article for Case A.",
        request_mode="literal",
        hold_for_approval=False,
        transport="telegram",
        delivery_target="123456",
        transport_message_id="101",
        requester_identity="telegram:999",
    )

    resp = process_herald_request(db_session, req)

    assert resp.status == JobState.QUEUED_TTS.value
    assert resp.is_duplicate is False
    assert resp.rerun_of_job_id is None

    job = db_session.query(PodcastJob).filter(PodcastJob.id == resp.job_id).first()
    assert job is not None
    assert job.status == JobState.QUEUED_TTS.value
    assert job.script_json is not None
    assert job.rerun_of_job_id is None
    assert job.generation_settings_json is not None
    assert job.generation_settings_json["mode"] == "literal"


def test_case_b_unique_with_approval(db_session):
    """Case B: Unique content, hold_for_approval=True -> AWAITING_APPROVAL with script generated."""
    req = HeraldRequest(
        source_text="This is a unique test article for Case B.",
        request_mode="literal",
        hold_for_approval=True,
        transport="telegram",
        delivery_target="123456",
        transport_message_id="102",
        requester_identity="telegram:999",
    )

    resp = process_herald_request(db_session, req)

    assert resp.status == JobState.AWAITING_APPROVAL.value
    assert resp.is_duplicate is False
    assert resp.rerun_of_job_id is None

    job = db_session.query(PodcastJob).filter(PodcastJob.id == resp.job_id).first()
    assert job is not None
    assert job.status == JobState.AWAITING_APPROVAL.value
    assert job.script_json is not None
    assert job.rerun_of_job_id is None


def test_case_c_duplicate_with_approval(db_session):
    """Case C: Duplicate content, hold_for_approval=True -> AWAITING_APPROVAL with rerun lineage."""
    text = "Duplicate content for testing Case C and Case D."

    # 1. Process initial job to completion
    req1 = HeraldRequest(
        source_text=text,
        request_mode="literal",
        hold_for_approval=False,
        transport="telegram",
        delivery_target="123456",
        transport_message_id="103",
        requester_identity="telegram:999",
    )
    resp1 = process_herald_request(db_session, req1)
    job1 = db_session.query(PodcastJob).filter(PodcastJob.id == resp1.job_id).first()
    job1.status = JobState.COMPLETE.value
    db_session.commit()

    # 2. Second request with duplicate content and hold_for_approval=True
    req2 = HeraldRequest(
        source_text=text,
        request_mode="literal",
        hold_for_approval=True,
        transport="telegram",
        delivery_target="123456",
        transport_message_id="104",  # distinct transport message
        requester_identity="telegram:999",
    )
    resp2 = process_herald_request(db_session, req2)

    assert resp2.job_id != resp1.job_id
    assert resp2.status == JobState.AWAITING_APPROVAL.value
    assert resp2.is_duplicate is True
    assert resp2.rerun_of_job_id == resp1.job_id

    job2 = db_session.query(PodcastJob).filter(PodcastJob.id == resp2.job_id).first()
    assert job2 is not None
    assert job2.rerun_of_job_id == job1.id
    assert job2.status == JobState.AWAITING_APPROVAL.value
    assert job2.script_json is not None

    # Prior job remains immutable
    db_session.refresh(job1)
    assert job1.status == JobState.COMPLETE.value


def test_case_d_duplicate_no_approval_awaiting_rerun_confirmation(db_session):
    """Case D: Duplicate content, hold_for_approval=False -> AWAITING_RERUN_CONFIRMATION without scripting."""
    text = "Duplicate content for Case D verification."

    # 1. Prior job
    req1 = HeraldRequest(
        source_text=text,
        request_mode="literal",
        hold_for_approval=False,
        transport="telegram",
        delivery_target="123456",
        transport_message_id="105",
        requester_identity="telegram:999",
    )
    resp1 = process_herald_request(db_session, req1)
    job1 = db_session.query(PodcastJob).filter(PodcastJob.id == resp1.job_id).first()
    job1.status = JobState.COMPLETE.value
    db_session.commit()

    # 2. Duplicate request with hold_for_approval=False
    req2 = HeraldRequest(
        source_text=text,
        request_mode="literal",
        hold_for_approval=False,
        transport="telegram",
        delivery_target="123456",
        transport_message_id="106",
        requester_identity="telegram:999",
    )
    resp2 = process_herald_request(db_session, req2)

    assert resp2.job_id != resp1.job_id
    assert resp2.status == JobState.AWAITING_RERUN_CONFIRMATION.value
    assert resp2.is_duplicate is True
    assert resp2.rerun_of_job_id == resp1.job_id

    job2 = db_session.query(PodcastJob).filter(PodcastJob.id == resp2.job_id).first()
    assert job2 is not None
    assert job2.status == JobState.AWAITING_RERUN_CONFIRMATION.value
    # SCRIPT MUST NOT BE GENERATED YET
    assert job2.script_json is None
    assert job2.rerun_of_job_id == job1.id

    # 3. Simulating user approving the rerun
    resp3 = execute_script_generation(db_session, job2, hold_for_approval=False)
    assert resp3.status == JobState.QUEUED_TTS.value

    db_session.refresh(job2)
    assert job2.status == JobState.QUEUED_TTS.value
    assert job2.script_json is not None

    # Prior job remains immutable
    db_session.refresh(job1)
    assert job1.status == JobState.COMPLETE.value
