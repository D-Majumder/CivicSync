from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, status
from google.genai.errors import APIError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ai.client import analyze_complaint
from ai.schemas import CivicIssue
from backend.assignment_rules import (
    AssignmentNotAllowedError,
    DepartmentInactiveError,
    DepartmentNotFoundError,
)
from backend.database import get_db
from backend.models import Department, Issue
from backend.repository import (
    get_active_departments,
    get_assignment_history,
    get_current_assignment,
    get_issue_by_public_id,
    get_status_history,
)
from backend.schemas import (
    AnalyzeRequest,
    AssignmentHistoryEntryResponse,
    AssignmentRequest,
    AssignmentResponse,
    DepartmentResponse,
    IssueResponse,
    StatusHistoryEntryResponse,
    StatusUpdateRequest,
)
from backend.service import assign_issue_department, submit_complaint, transition_issue
from backend.transitions import InvalidTransitionError

# Load environment variables from .env before anything else (e.g. ai/client.py)
# reads them. Must run before any AI service call, since GEMINI_API_KEY is
# read from the environment at call time.
load_dotenv()

app = FastAPI(
    title="CivicSync API",
    description="AI-powered civic intelligence platform API",
    version="0.1.0",
)


@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "service": "CivicSync API",
        "version": "0.1.0",
    }


@app.post(
    "/api/analyze",
    response_model=CivicIssue,
    status_code=status.HTTP_200_OK,
    summary="Analyze a citizen complaint into a structured civic issue",
)
def analyze(request: AnalyzeRequest) -> CivicIssue:
    """Convert raw citizen complaint text into a structured CivicIssue.

    All Gemini/AI logic lives in ai/client.py; this route only validates
    the request, delegates to the existing AI service, and translates
    known failure modes into clean HTTP responses.
    """
    try:
        return analyze_complaint(request.text)
    except ValueError as exc:
        # e.g. blank text (belt-and-suspenders alongside AnalyzeRequest's own
        # validation) or an empty/unparsable Gemini response.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from None
    except EnvironmentError:
        # Misconfiguration (e.g. missing GEMINI_API_KEY). Never echo the
        # underlying message back to the client -- it could reference env
        # var contents or local setup details.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AI service is not configured correctly.",
        ) from None
    except APIError:
        # Gemini-side failure (network, auth, rate limit, server error, etc).
        # google-genai's APIError messages can include request/response
        # internals, so respond with a generic message instead of str(exc).
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI service is temporarily unavailable. Please try again shortly.",
        ) from None


@app.post(
    "/api/issues",
    response_model=IssueResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Submit a citizen complaint and persist it as a civic issue",
)
def submit_issue(request: AnalyzeRequest, db: Session = Depends(get_db)) -> Issue:
    """Analyze a citizen complaint and persist it as a new Issue (status
    SUBMITTED). All AI and database logic lives in backend/service.py and
    backend/repository.py; this route only validates the request,
    delegates, and translates known failure modes into HTTP responses.
    """
    try:
        return submit_complaint(db, request.text)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from None
    except EnvironmentError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AI service is not configured correctly.",
        ) from None
    except APIError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="AI service is temporarily unavailable. Please try again shortly.",
        ) from None
    except SQLAlchemyError:
        # Never echo str(exc) here -- SQLAlchemy error messages can include
        # the underlying SQL statement, connection details, or file paths.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to save the issue. Please try again.",
        ) from None


@app.get(
    "/api/issues/{public_id}",
    response_model=IssueResponse,
    status_code=status.HTTP_200_OK,
    summary="Retrieve a persisted civic issue by its public id",
    responses={status.HTTP_404_NOT_FOUND: {"description": "Issue not found."}},
)
def read_issue(public_id: str, db: Session = Depends(get_db)) -> Issue:
    """Look up a previously submitted Issue. Does not call Gemini."""
    issue = get_issue_by_public_id(db, public_id)
    if issue is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Issue not found.",
        )
    return issue


@app.post(
    "/api/issues/{public_id}/status",
    response_model=IssueResponse,
    status_code=status.HTTP_200_OK,
    summary="Transition a civic issue to a new status",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Issue not found."},
        status.HTTP_400_BAD_REQUEST: {
            "description": "The requested status transition is not permitted."
        },
    },
)
def update_issue_status(
    public_id: str, request: StatusUpdateRequest, db: Session = Depends(get_db)
) -> Issue:
    """Validate and apply a status transition, recording status history.

    Transition rules live in backend/transitions.py; persistence lives in
    backend/repository.py; backend/service.py orchestrates the lookup +
    transition. This route only translates known failure modes into HTTP
    responses. Does not call Gemini -- AI never drives status transitions.
    """
    try:
        issue = transition_issue(db, public_id, request.status, request.reason)
    except InvalidTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to update issue status. Please try again.",
        ) from None

    if issue is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Issue not found.",
        )
    return issue


@app.get(
    "/api/issues/{public_id}/history",
    response_model=list[StatusHistoryEntryResponse],
    status_code=status.HTTP_200_OK,
    summary="Retrieve the status history for a civic issue",
    responses={status.HTTP_404_NOT_FOUND: {"description": "Issue not found."}},
)
def read_issue_status_history(
    public_id: str, db: Session = Depends(get_db)
) -> list[StatusHistoryEntryResponse]:
    """Return an Issue's status history, oldest first. Does not call Gemini."""
    issue = get_issue_by_public_id(db, public_id)
    if issue is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Issue not found.",
        )
    return get_status_history(db, issue)


@app.get(
    "/api/departments",
    response_model=list[DepartmentResponse],
    status_code=status.HTTP_200_OK,
    summary="List active departments in the controlled registry",
)
def list_departments(db: Session = Depends(get_db)) -> list[Department]:
    """Return active departments only. Does not call Gemini. The registry
    is not end-user-creatable here -- it's seeded once via Alembic."""
    return get_active_departments(db)


@app.post(
    "/api/issues/{public_id}/assignment",
    response_model=IssueResponse,
    status_code=status.HTTP_200_OK,
    summary="Officially assign (or reassign) a civic issue to a department",
    responses={
        status.HTTP_404_NOT_FOUND: {"description": "Issue or department not found."},
        status.HTTP_400_BAD_REQUEST: {
            "description": (
                "Department is inactive, or the issue's current status "
                "doesn't permit assignment."
            )
        },
    },
)
def assign_issue(
    public_id: str, request: AssignmentRequest, db: Session = Depends(get_db)
) -> Issue:
    """Assign an issue to an official Department from the registry.

    This is always an explicit authorized action -- it is never derived
    from the AI's suggested_department. If the issue is CLASSIFIED, this
    also transitions it to ROUTED (via the existing centralized transition
    system); ROUTED-or-later issues are reassignable without altering
    their status. Transition rules, assignment eligibility, and
    persistence live in backend/transitions.py, backend/assignment_rules.py,
    and backend/repository.py; this route only translates known failure
    modes into HTTP responses. Does not call Gemini.
    """
    try:
        issue = assign_issue_department(db, public_id, request.department_code, request.reason)
    except DepartmentNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from None
    except DepartmentInactiveError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from None
    except AssignmentNotAllowedError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from None
    except SQLAlchemyError:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to assign the issue. Please try again.",
        ) from None

    if issue is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Issue not found.",
        )
    return issue


@app.get(
    "/api/issues/{public_id}/assignment",
    response_model=AssignmentResponse | None,
    status_code=status.HTTP_200_OK,
    summary="Get the current official department assignment for an issue",
    responses={status.HTTP_404_NOT_FOUND: {"description": "Issue not found."}},
)
def read_current_assignment(public_id: str, db: Session = Depends(get_db)):
    """Return the current assignment, or a bare `200` with JSON `null` if
    the issue exists but has never been assigned -- that's a normal,
    expected state, not an error. 404 only if the issue itself doesn't
    exist. Does not call Gemini."""
    issue = get_issue_by_public_id(db, public_id)
    if issue is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Issue not found.",
        )
    current = get_current_assignment(db, issue)
    if current is None:
        return None
    return AssignmentResponse(
        code=current.department.code,
        name=current.department.name,
        assigned_at=current.assigned_at,
        reason=current.reason,
    )


@app.get(
    "/api/issues/{public_id}/assignment/history",
    response_model=list[AssignmentHistoryEntryResponse],
    status_code=status.HTTP_200_OK,
    summary="Get the full official assignment history for an issue",
    responses={status.HTTP_404_NOT_FOUND: {"description": "Issue not found."}},
)
def read_assignment_history(
    public_id: str, db: Session = Depends(get_db)
) -> list[AssignmentHistoryEntryResponse]:
    """Return all assignment history entries, oldest first. Does not call
    Gemini."""
    issue = get_issue_by_public_id(db, public_id)
    if issue is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Issue not found.",
        )
    return [
        AssignmentHistoryEntryResponse(
            department_code=entry.department.code,
            department_name=entry.department.name,
            assigned_at=entry.assigned_at,
            unassigned_at=entry.unassigned_at,
            reason=entry.reason,
        )
        for entry in get_assignment_history(db, issue)
    ]