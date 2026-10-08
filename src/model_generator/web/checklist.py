"""Owner-private checklist and remediation derived from verified final bytes."""
import json
import re
from .diagnostic_catalog import KNOWN_CHECKS, NOT_IMPLEMENTED, PHASE_TITLES, RULE_TITLES, rule_help
from .progress import aggregate,validate_snapshot
from .reporting import sanitize_report,REMOVED
from .journal import correlation
from .security import ApiError


def applicable(kind):
    phases=['input.archive','input.fbx'] if kind=='zip-fbx' else ['input.package',*[key for key in PHASE_TITLES if key.startswith('scene.')]]
    rules=[key for key in RULE_TITLES if (key.startswith(('zip.','fbx.','png.','profile.')) or key in {'package.fbx_count','package.ground','package.wrapper'})] if kind=='zip-fbx' else [key for key in RULE_TITLES if key.startswith('package.') and key not in {'package.fbx_count','package.ground','package.wrapper'}]
    return phases+rules+list(NOT_IMPLEMENTED)


def presentation(job, snapshot=None, report=None):
    final=report is not None
    safe_code=re.compile(r"[a-z][a-z0-9_.]{0,79}\Z")
    evidence={}
    if final:
        source=report.get('check_evidence')
        if source:
            validate_snapshot(source,job['input_sha256'],source['attempt'])
            evidence={row['id']:row for row in source['checks'] if row['id'] in PHASE_TITLES}
        findings=report.get('findings',[])
        actual=aggregate(findings)
        for finding in findings:
            code=finding.get('rule_id')
            if isinstance(code,str) and safe_code.fullmatch(code) and code not in KNOWN_CHECKS:
                value=actual.setdefault(code,{'state':'not_checked','count':0})
                value['count']+=1
                if finding.get('status')=='fail': value['state']='failed'
        explicit={row['id']:row for row in source['checks']} if source else {}
        for code,row in actual.items():
            state=row['state']
            if state!='failed' and explicit.get(code,{}).get('state')=='not_checked': state='not_checked'
            # A retained prefix does not prove full-domain success.
            if report.get('report_truncated') and state!='failed': state='not_checked'
            evidence[code]={'id':code,**row,'state':state,'sequence':source['sequence'] if source else None,
                            'completedAt':explicit.get(code,{}).get('completedAt',job['updated_at'])}
        if report.get('report_truncated'):
            for row in evidence.values():
                if row['state']!='failed': row['state']='not_checked'
    elif snapshot and snapshot['attempt']==job['worker_epoch']:
        validate_snapshot(snapshot,job['input_sha256'],job['worker_epoch'])
        evidence={row['id']:row for row in snapshot['checks']}
    rows=[]
    terminal=job['state'] not in {'queued','running'}
    for code in dict.fromkeys([*applicable(job['input_kind']),*evidence]):
        item=evidence.get(code)
        state=item['state'] if item else 'not_checked' if final or terminal or code in NOT_IMPLEMENTED else 'waiting'
        if state in {'checking','waiting'} and (terminal or final): state='not_checked'
        rows.append({'id':code,'title':rule_help(code)['title'],'state':state,
                     'scope':'profile' if code.startswith('profile.') or code=='current_regulatory_applicability' else 'external' if code in {'architecture_council_defence','approval_for_vpm'} else 'procedure' if code in NOT_IMPLEMENTED else 'technical',
                     'count':item['count'] if item else 0,'sequence':item['sequence'] if item else None,
                     'completedAt':item['completedAt'] if item and state not in {'waiting','checking'} else None,
                     'summary':'Нет подтверждения завершённой проверки.' if state=='not_checked' else 'Отметка относится к прочитанному входу и указанной попытке.',
                     'detailsUrl':f"/api/jobs/{job['id']}/checks/{code}"})
    shown=len(report.get('findings',[])) if final else 0
    original=report.get('original_findings_count',shown) if final else 0
    return {'jobId':job['id'],'inputHash':job['input_sha256'],'attempt':job['worker_epoch'],
            'state':job['state'],'reportAttempt':report.get('check_evidence',{}).get('attempt') if final else None,'authoritative':final,'checks':rows,'diagnosticId':correlation('job',job['id'],job['worker_epoch'] or ''),
            'coverage':report.get('coverage',job['coverage']) if final else job['coverage'],
            'findings':{'shown':shown,'original':original,'omitted':max(0,original-shown),'truncated':bool(final and report.get('report_truncated'))},
            'limitExplanation':'Если список сокращён, разделите исходные данные на меньшие согласованные пакеты и проверьте каждый; пропущенные результаты не считаются успешными.'}


def owned_context(db,storage,owner,job_id,now):
    from .jobs import JobRepository
    from .artifacts import owned_artifact_descriptor
    with db.connect() as con:
        job=JobRepository(db,storage.settings)._row(con,owner,job_id,now)
        progress=con.execute('SELECT snapshot FROM job_progress WHERE job_id=%s AND attempt=%s',(job_id,job['worker_epoch'])).fetchone()
        artifact=con.execute("SELECT id FROM artifacts WHERE job_id=%s AND state='ready' AND kind='report'",(job_id,)).fetchone()
    descriptor=None
    if artifact and job['state'] in {'completed','failed'} and not job['cancel_requested']:
        descriptor=owned_artifact_descriptor(db,storage,owner,job_id,artifact['id'],now)
    return job,progress['snapshot'] if progress else None,descriptor


def read_report(storage,descriptor):
    from .artifacts import open_descriptor,verified_chunks
    stream,item=open_descriptor(storage,descriptor)
    try:
        wire=b''.join(verified_chunks(stream,item))
        report=json.loads(wire)
        if not isinstance(report,dict): raise ValueError
        # Existing sanitizer counters are retained, never reclassify trusted bytes.
        if len(wire)>storage.settings.report_max_bytes or len(report.get('findings',[]))>storage.settings.report_max_findings: raise ValueError
        return report
    except (ValueError,TypeError,RecursionError,UnicodeError):
        raise ApiError('storage_unavailable','Verified report is unavailable.',503) from None
    finally: stream.close()


def safe_value(value):
    result=sanitize_report({'findings':[],'value':value},max_bytes=16384).get('value')
    def markup(item):
        if isinstance(item,str) and ('<' in item or '>' in item or any(ord(c)<32 for c in item)): return REMOVED
        if isinstance(item,list): return [markup(v) for v in item]
        if isinstance(item,dict): return {key:markup(v) for key,v in item.items() if '<' not in key and '>' not in key}
        return item
    return markup(result)


def details(job,snapshot,report,code,offset,limit):
    known=code in KNOWN_CHECKS or (isinstance(code,str) and re.fullmatch(r'[a-z][a-z0-9_.]{0,79}',code) and any(f.get('rule_id')==code for f in (report or {}).get('findings',[])))
    if not known or type(offset) is not int or type(limit) is not int or not 0<=offset<=10000 or not 1<=limit<=100:
        raise ApiError('invalid_cursor','Check parameters are invalid.',422)
    envelope=presentation(job,snapshot,report)
    phase_prefixes={'input.archive':('zip.',),'input.fbx':('fbx.','png.','profile.'),
                    'input.package':('package.',),'scene.complete':('package.',)}
    phase_rules={'scene.links':{'package.links','package.link'},'scene.materials':{'package.materials','package.material'},
                 'scene.meshes':{'package.geometry','package.materials','package.uv','package.normals','package.material_appearance'},
                 'scene.coordinates':{'package.coordinates','package.coordinate_control'},
                 'scene.ifc':{'package.ifc','package.ifc_semantics'}}
    matching=[item for item in (report or {}).get('findings',[]) if item.get('rule_id')==code
              or (code in phase_prefixes and str(item.get('rule_id','')).startswith(phase_prefixes[code]))
              or item.get('rule_id') in phase_rules.get(code,set())]
    selected=[]
    for finding in matching[offset:offset+limit]:
        actual=finding.get('actual')
        key=actual.get('element_key') if isinstance(actual,dict) else None
        selected.append({'ruleId':finding.get('rule_id'),**rule_help(finding.get('rule_id')),'status':finding.get('status'),'observed':safe_value(actual),
                         'expected':safe_value(finding.get('expected')),'file':safe_value(finding.get('file')),
                         'elementKey':safe_value(key),'message':safe_value(finding.get('message')),
                         'locationExplanation':'Связь с элементом исходной модели не подтверждена.' if not key else 'Идентификатор взят из пакета; связь с открытой моделью Revit отдельно не проверена.'})
    return {'jobId':job['id'],'inputHash':job['input_sha256'],'checkId':code,**rule_help(code),
            'state':next((row['state'] for row in envelope['checks'] if row['id']==code),'not_checked'),
            'authoritative':envelope['authoritative'],'findings':selected,'shownForCheck':len(matching),
            'counts':envelope['findings'],'nextOffset':offset+limit if offset+limit<len(matching) else None,
            'diagnosticId':envelope['diagnosticId'],'limitExplanation':envelope['limitExplanation']}
