"""Authenticated receive-bounded durable job routes."""
from fastapi import APIRouter, Request
from starlette.responses import JSONResponse, Response
from .uploads import owned_io
from .auth import require_user, require_mutation
from .security import ApiError, bounded_json

router = APIRouter(prefix='/api/jobs')


@router.post('')
async def create(request: Request):
    owner = await require_mutation(request)
    state = request.app.state
    value = await bounded_json(request, state.settings.json_max_bytes, state.settings.json_idle_seconds, state.settings.json_wall_seconds)
    if set(value) != {'uploadId', 'region', 'procedure', 'submissionDate'}:
        raise ApiError('invalid_job', 'Job parameters are invalid.', 422)
    prepared=await state.db.run(state.jobs.prepare_create,owner.id,value['uploadId'],value['region'],value['procedure'],value['submissionDate'])
    # No await between admission check and increment: one API event loop owns this counter.
    if state.job_fingerprint_active>=2:
        raise ApiError('service_busy','Service is temporarily unavailable.',503)
    state.job_fingerprint_active+=1
    try:
        digest=await owned_io(state.storage,state.jobs.fingerprint_prepared,prepared)
    finally:
        # owned_io joins the physical hash callback before propagating cancellation.
        state.job_fingerprint_active-=1
    result=await state.db.run(state.jobs.create_prepared,prepared,digest,int(state.clock()))
    return JSONResponse(result, status_code=201)


@router.get('')
async def listing(request: Request):
    owner = await require_user(request)
    try:
        limit = int(request.query_params.get('limit', '20'))
    except ValueError:
        raise ApiError('invalid_cursor', 'List parameters are invalid.', 422) from None
    return await request.app.state.db.run(request.app.state.jobs.list_owned, owner.id, limit, request.query_params.get('cursor'), int(request.app.state.clock()))


@router.get('/{job_id}')
async def get(request: Request, job_id: str):
    owner = await require_user(request)
    return await request.app.state.db.run(request.app.state.jobs.get_owned, owner.id, job_id, int(request.app.state.clock()))


@router.post('/{job_id}/cancel')
async def cancel(request: Request, job_id: str):
    owner = await require_mutation(request)
    status, value = await request.app.state.db.run(request.app.state.jobs.request_cancel, owner.id, job_id, int(request.app.state.clock()))
    return JSONResponse(value, status_code=status)


@router.delete('/{job_id}')
async def delete(request: Request, job_id: str):
    owner = await require_mutation(request)
    status, value = await request.app.state.db.run(request.app.state.jobs.request_delete, owner.id, job_id, int(request.app.state.clock()))
    return Response(status_code=204) if status==204 else JSONResponse(value, status_code=status)


@router.get('/{job_id}/artifacts/{artifact_id}')
async def artifact(request: Request, job_id: str, artifact_id: str):
    from .artifacts import open_descriptor,ArtifactResponse
    owner=await require_user(request); state=request.app.state
    if request.headers.get('range'):
        # Authorize before reporting supported download capabilities.
        from .artifacts import owned_artifact_descriptor
        await state.db.run(owned_artifact_descriptor,state.db,state.storage,owner.id,job_id,artifact_id,int(state.clock()))
        raise ApiError('range_unavailable','Range requests are unavailable.',416)
    # DB admission/authorization finishes before off-thread S3 HEAD and GET.
    from .artifacts import owned_artifact_descriptor
    item=await state.db.run(owned_artifact_descriptor,state.db,state.storage,owner.id,job_id,artifact_id,int(state.clock()))
    stream,item=await owned_io(state.storage,open_descriptor,state.storage,item,cancel_cleanup=lambda result:result[0].close())
    return ArtifactResponse(state.storage,stream,item)


async def _check_context(request, job_id):
    from .checklist import owned_context, read_report
    owner=await require_user(request)
    state=request.app.state
    job,snapshot,descriptor=await state.db.run(owned_context,state.db,state.storage,owner.id,job_id,int(state.clock()))
    report=await owned_io(state.storage,read_report,state.storage,descriptor) if descriptor else None
    if report is not None and report.get('input_sha256')!=job['input_sha256']:
        raise ApiError('storage_unavailable','Verified report is unavailable.',503)
    return job,snapshot,report


@router.get('/{job_id}/checks')
async def checks(request: Request,job_id: str):
    from .checklist import presentation
    return presentation(*(await _check_context(request,job_id)))


@router.get('/{job_id}/checks/{check_id}')
async def check_details(request: Request,job_id: str,check_id: str):
    from .checklist import details
    context=await _check_context(request,job_id)
    try:
        offset=int(request.query_params.get('offset','0'))
        limit=int(request.query_params.get('limit','50'))
    except ValueError:
        raise ApiError('invalid_cursor','Check parameters are invalid.',422) from None
    return details(*context,check_id,offset,limit)
