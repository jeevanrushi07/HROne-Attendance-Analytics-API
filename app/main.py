"""Employee Attendance & Analytics API, contract v2.0."""
import json
import os, re, calendar
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError
from bson import json_util

load_dotenv()
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=2500, tz_aware=False)
db = client[MONGO_DB]
IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


def create_indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    db.employees.create_index([("joined_on", ASCENDING)], name="ix_employee_joined")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="ix_attendance_employee_date_status")
    db.attendance_logs.create_index([("date", ASCENDING), ("status", ASCENDING), ("late_minutes", DESCENDING)], name="ix_attendance_date_status_late")

@app.on_event("startup")
def startup():
    try:
        create_indexes()
    except Exception:
        # Keep the process alive so /health can report 503 while MongoDB is unavailable.
        pass


def now_utc(): return datetime.now(UTC).replace(microsecond=0)
def epoch_ms(dt):
    if dt is None: return None
    if dt.tzinfo is None: dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)
def from_ms(value: int):
    if isinstance(value, bool) or not isinstance(value, int) or value < 100000000000 or value > 4102444800000:
        raise HTTPException(422, "timestamp must be an epoch-millisecond integer in the supported range")
    return datetime.fromtimestamp(value / 1000, UTC).replace(microsecond=0)
def utc_aware(dt): return dt.replace(tzinfo=UTC) if dt and dt.tzinfo is None else dt
def to_ist(dt): return utc_aware(dt).astimezone(IST)
def iso_date(value):
    try:
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value: raise ValueError()
        return parsed
    except (ValueError, TypeError): raise HTTPException(422, "date must be YYYY-MM-DD")
def parse_month(month):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    y, m = map(int, month.split("-")); start = date(y,m,1)
    end = date(y,m,calendar.monthrange(y,m)[1])
    return start, end
def shift_dt(day: date, hhmm: str):
    h,m=map(int,hhmm.split(":")); return datetime.combine(day,time(h,m),IST)
def attendance_date(punch_in, emp):
    local = to_ist(punch_in); day=local.date()
    if emp["shift_end"] <= emp["shift_start"] and local.strftime("%H:%M") < emp["shift_end"]:
        day -= timedelta(days=1)
    return day.isoformat()
def shift_bounds(day_str, emp):
    day=iso_date(day_str)
    start=shift_dt(day, emp["shift_start"])
    end_day=day + (timedelta(days=1) if emp["shift_end"] <= emp["shift_start"] else timedelta())
    end=shift_dt(end_day, emp["shift_end"])
    return start.astimezone(UTC), end.astimezone(UTC)
def compute_late_minutes(punch_in, shift_start, record_date=None, shift_end=None):
    local=to_ist(punch_in); d=iso_date(record_date) if record_date else local.date()
    start=shift_dt(d, shift_start).astimezone(UTC)
    seconds=(utc_aware(punch_in)-start).total_seconds()
    return int(seconds//60) if seconds > 600 else 0
def compute_work_hours(punch_in,punch_out):
    seconds=Decimal(str((utc_aware(punch_out)-utc_aware(punch_in)).total_seconds()))
    return float((seconds/Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
def compute_overtime(punch_out, shift_end, date_str, shift_start=None):
    day=iso_date(date_str); end_day=day + (timedelta(days=1) if shift_start and shift_end <= shift_start else timedelta())
    end=shift_dt(end_day,shift_end).astimezone(UTC)
    mins=int((utc_aware(punch_out)-end).total_seconds()//60)
    return mins if mins >= 30 else 0
def rounded(value, places=2):
    if value is None: return None
    return float(Decimal(str(value)).quantize(Decimal("1."+"0"*places), rounding=ROUND_HALF_UP))

def same_value(left, right):
    """Compare datetimes by instant, regardless of naive/aware UTC representation."""
    if isinstance(left, datetime) and isinstance(right, datetime):
        return epoch_ms(left) == epoch_ms(right)
    return left == right
def serial_employee(d):
    out={k:v for k,v in d.items() if k != "_id"}
    if "created_at" in out: out["created_at"]=epoch_ms(out["created_at"])
    return out
def serial_record(d):
    out={k:v for k,v in d.items() if k != "_id"}
    for k in ("punch_in","punch_out"):
        out[k]=epoch_ms(out.get(k))
    out["late_minutes"]=int(out.get("late_minutes") or 0); out["overtime_minutes"]=int(out.get("overtime_minutes") or 0)
    out["half_day"]=bool(out.get("half_day",False)); out["history"]=out.get("history",[])
    for h in out["history"]:
        if "at" in h and isinstance(h["at"],datetime): h["at"]=epoch_ms(h["at"])
        for change in h.get("changes",{}).values():
            for side in ("from","to"):
                if isinstance(change.get(side),datetime): change[side]=epoch_ms(change[side])
    return out
def json_safe(value):
    """Convert BSON explain output to ordinary JSON-compatible values.

    Use json.loads rather than json_util.loads so BSON-specific values such as
    Timestamp remain Extended JSON objects instead of non-serializable Python
    objects that FastAPI cannot encode.
    """
    return json.loads(
        json_util.dumps(value, json_options=json_util.RELAXED_JSON_OPTIONS)
    )
def require_page(page,page_size):
    if page < 1 or page_size < 1 or page_size > 100: raise HTTPException(422,"page must be >= 1 and page_size must be 1..100")
def workdays(start: date,end: date):
    if end < start:return 0
    count=0; d=start
    while d<=end:
        if d.weekday()<5: count+=1
        d+=timedelta(days=1)
    return count

class StrictModel(BaseModel):
    model_config=ConfigDict(extra="ignore")

class EmployeeIn(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1,max_length=100)
    email: str = Field(max_length=120,pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1,max_length=50)
    shift_start: str = Field(default="09:30",pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30",pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str
    @field_validator("joined_on")
    @classmethod
    def valid_joined(cls,v):
        try:
            if date.fromisoformat(v).isoformat()!=v: raise ValueError()
        except Exception: raise ValueError("joined_on must be YYYY-MM-DD")
        return v
    @model_validator(mode="after")
    def shift_differs(self):
        if self.shift_start==self.shift_end: raise ValueError("shift_start must differ from shift_end")
        return self

class PunchInIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True)
    status: Literal["PRESENT","WFH","ON_DUTY"] = "PRESENT"
    @field_validator("punched_at")
    @classmethod
    def valid_ts(cls,v):
        if v is not None and (type(v) is not int or v<100000000000 or v>4102444800000): raise ValueError("punched_at must be epoch milliseconds")
        return v
class PunchOutIn(StrictModel):
    emp_code: str
    punched_at: Optional[int] = Field(default=None, strict=True)
    @field_validator("punched_at")
    @classmethod
    def valid_ts(cls,v):
        if v is not None and (type(v) is not int or v<100000000000 or v>4102444800000): raise ValueError("punched_at must be epoch milliseconds")
        return v
class RegularizeIn(StrictModel):
    status: Optional[Literal["PRESENT","WFH","ON_DUTY","ABSENT","LEAVE"]]=None
    punch_in: Optional[int]=Field(default=None, strict=True)
    punch_out: Optional[int]=Field(default=None, strict=True)
    reason: str=Field(min_length=5,max_length=200)
    regularized_by: str=Field(min_length=1,max_length=50)
    @field_validator("punch_in","punch_out")
    @classmethod
    def valid_ts(cls,v):
        if v is not None and (type(v) is not int or v<100000000000 or v>4102444800000): raise ValueError("timestamp must be epoch milliseconds")
        return v

@app.get("/health")
def health():
    try: client.admin.command("ping"); return {"status":"ok"}
    except Exception: raise HTTPException(503,"MongoDB unavailable")

@app.post("/employees",status_code=201)
def create_employee(body: EmployeeIn):
    doc=body.model_dump(); doc["created_at"]=now_utc()
    try: db.employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,"emp_code already exists")
    return serial_employee(doc)

@app.get("/employees")
def list_employees(department: Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    q={"department":department} if department is not None else {}
    total=db.employees.count_documents(q)
    docs=db.employees.find(q,{"_id":0}).sort("emp_code",ASCENDING).skip((page-1)*page_size).limit(page_size)
    return {"items":[serial_employee(d) for d in docs],"total":total,"page":page,"page_size":page_size}

@app.post("/attendance/punch-in",status_code=201)
def punch_in(body:PunchInIn):
    emp=db.employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404,"employee not found")
    ts=from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    d=attendance_date(ts,emp)
    doc={"emp_code":body.emp_code,"date":d,"status":body.status,"punch_in":ts,"punch_out":None,"work_hours":None,
         "late_minutes":compute_late_minutes(ts,emp["shift_start"],d,emp["shift_end"]),"overtime_minutes":0,"half_day":False,"history":[]}
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,"already punched in for this date")
    return serial_record(doc)

@app.post("/attendance/punch-out")
def punch_out(body:PunchOutIn):
    emp=db.employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404,"employee not found")
    ts=from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    rec=db.attendance_logs.find_one({"emp_code":body.emp_code,"punch_in":{"$ne":None,"$lte":ts}},sort=[("punch_in",DESCENDING)])
    if not rec: raise HTTPException(404,"no punch-in found")
    if rec.get("punch_out") is not None: raise HTTPException(409,"attendance already punched out")
    if ts<=utc_aware(rec["punch_in"]) or (ts-utc_aware(rec["punch_in"])).total_seconds()>86400: raise HTTPException(422,"punch-out must be after punch-in and within 24 hours")
    hours=compute_work_hours(rec["punch_in"],ts); overtime=compute_overtime(ts,emp["shift_end"],rec["date"],emp["shift_start"])
    updated=db.attendance_logs.find_one_and_update({"emp_code":body.emp_code,"date":rec["date"],"punch_out":None},
       {"$set":{"punch_out":ts,"work_hours":hours,"overtime_minutes":overtime,"half_day":hours<4.50}},return_document=ReturnDocument.AFTER)
    if not updated: raise HTTPException(409,"attendance already punched out")
    return serial_record(updated)

@app.get("/attendance")
def list_attendance(emp_code:Optional[str]=None,date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[str]=None,
                    page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    if date_from: iso_date(date_from)
    if date_to: iso_date(date_to)
    if date_from and date_to and date_from>date_to: raise HTTPException(422,"date_from must be <= date_to")
    q={}
    if emp_code is not None:q["emp_code"]=emp_code
    if status is not None:
        if status not in STATUSES: raise HTTPException(422,"invalid status")
        q["status"]=status
    if date_from or date_to:
        q["date"]={}
        if date_from:q["date"]["$gte"]=date_from
        if date_to:q["date"]["$lte"]=date_to
    total=db.attendance_logs.count_documents(q)
    docs=db.attendance_logs.find(q).sort([("date",DESCENDING),("emp_code",ASCENDING)]).skip((page-1)*page_size).limit(page_size)
    return {"items":[serial_record(d) for d in docs],"total":total,"page":page,"page_size":page_size}

@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code:str,date:str,body:RegularizeIn):
    iso_date(date)
    emp=db.employees.find_one({"emp_code":emp_code})
    rec=db.attendance_logs.find_one({"emp_code":emp_code,"date":date})
    if not emp or not rec: raise HTTPException(404,"employee or attendance record not found")
    original={k:rec.get(k) for k in ("status","punch_in","punch_out","work_hours","late_minutes","overtime_minutes","half_day")}
    new={**original}
    supplied=body.model_fields_set
    if "status" in supplied:new["status"]=body.status
    if "punch_in" in supplied:new["punch_in"]=from_ms(body.punch_in) if body.punch_in is not None else None
    if "punch_out" in supplied:new["punch_out"]=from_ms(body.punch_out) if body.punch_out is not None else None
    if new["status"] in {"ABSENT","LEAVE"}:
        if ("punch_in" in supplied and body.punch_in is not None) or ("punch_out" in supplied and body.punch_out is not None): raise HTTPException(422,"ABSENT/LEAVE cannot have punch times")
        new["punch_in"]=None; new["punch_out"]=None; new["work_hours"]=None; new["late_minutes"]=0; new["overtime_minutes"]=0; new["half_day"]=False
    else:
        if new["punch_in"] is None: raise HTTPException(422,"presence status requires punch_in")
        if attendance_date(new["punch_in"],emp)!=date: raise HTTPException(422,"punch_in must belong to the attendance date")
        new["late_minutes"]=compute_late_minutes(new["punch_in"],emp["shift_start"],date,emp["shift_end"])
        if new["punch_out"] is not None:
            if utc_aware(new["punch_out"])<=utc_aware(new["punch_in"]) or (utc_aware(new["punch_out"])-utc_aware(new["punch_in"])).total_seconds()>86400: raise HTTPException(422,"invalid punch_out interval")
            new["work_hours"]=compute_work_hours(new["punch_in"],new["punch_out"])
            new["overtime_minutes"]=compute_overtime(new["punch_out"],emp["shift_end"],date,emp["shift_start"]); new["half_day"]=new["work_hours"]<4.5
        else:new["work_hours"]=None;new["overtime_minutes"]=0;new["half_day"]=False
    changes={}
    for k,v in new.items():
        old=original.get(k)
        if not same_value(old, v):
            if k in ("punch_in","punch_out"):
                changes[k]={"from":epoch_ms(old),"to":epoch_ms(v)}
            else:changes[k]={"from":old,"to":v}
    if not changes: raise HTTPException(422,"request does not change the record")
    entry={"at":now_utc(),"by":body.regularized_by,"reason":body.reason,"changes":changes}
    # Compare-and-swap protects concurrent updates and prevents lost audit entries.
    update={k:new[k] for k in new if not same_value(original.get(k), new[k])}
    cas_filter={"emp_code":emp_code,"date":date}
    if "history" in rec: cas_filter["history"]=rec["history"]
    else: cas_filter["history"]={"$exists":False}
    updated=db.attendance_logs.find_one_and_update(cas_filter,
       {"$set":update,"$push":{"history":entry}},return_document=ReturnDocument.AFTER)
    if not updated: raise HTTPException(409,"record changed concurrently; retry correction")
    return serial_record(updated)

# Shared aggregation expressions. MongoDB stores calendar dates as ISO strings, which sort lexically.
def month_match(month):
    start,end=parse_month(month); return {"$gte":start.isoformat(),"$lte":end.isoformat()}
def present_expr(): return {"$in":["$status",["PRESENT","WFH","ON_DUTY"]]}
def weekday_expr(date_expr): return {"$lt":[{"$subtract":[{"$dayOfWeek":{"date":{"$dateFromString":{"dateString":date_expr,"timezone":"Asia/Kolkata"}},"timezone":"Asia/Kolkata"}},1]},5]}

def monthly_pipeline(emp_code,month):
    start,end=parse_month(month)
    return [
      {"$match":{"emp_code":emp_code,"date":month_match(month)}},
      {"$set":{"_date":{"$dateFromString":{"dateString":"$date","timezone":"Asia/Kolkata"}},"_late":{"$ifNull":["$late_minutes",0]},"_ot":{"$ifNull":["$overtime_minutes",0]},"_half":{"$ifNull":["$half_day",False]}}},
      {"$group":{"_id":None,
       "present_days":{"$sum":{"$cond":[{"$and":[present_expr(),{"$lt":[{"$dayOfWeek":{"date":"$_date","timezone":"Asia/Kolkata"}},7]},{"$ne":[{"$dayOfWeek":{"date":"$_date","timezone":"Asia/Kolkata"}},1]}]}, {"$cond":["$_half",0.5,1]},0]}},
       "leave_days":{"$sum":{"$cond":[{"$eq":["$status","LEAVE"]},1,0]}},
       "late_count":{"$sum":{"$cond":[{"$gt":["$_late",0]},1,0]}},"total_late_minutes":{"$sum":"$_late"},"total_overtime_minutes":{"$sum":"$_ot"}}}
    ]

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code:str,month:str=Query(...,pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    emp=db.employees.find_one({"emp_code":emp_code})
    if not emp: raise HTTPException(404,"employee not found")
    start,end=parse_month(month); wd_start=max(start,iso_date(emp["joined_on"]))
    working=workdays(wd_start,end)
    rows=list(db.attendance_logs.aggregate(monthly_pipeline(emp_code,month)))
    stats=rows[0] if rows else {}
    present=float(stats.get("present_days",0))
    pct=rounded(Decimal(str(present))*100/Decimal(working),4) if working else None
    return {"emp_code":emp_code,"month":month,"working_days":working,"present_days":rounded(present),"leave_days":int(stats.get("leave_days",0)),"late_count":int(stats.get("late_count",0)),"total_late_minutes":int(stats.get("total_late_minutes",0)),"total_overtime_minutes":int(stats.get("total_overtime_minutes",0)),"attendance_pct":pct}

def department_summary_pipeline(month, department=None):
    start, end = parse_month(month)
    return [{"$match":{"joined_on":{"$lte":end.isoformat()},**({"department":department} if department is not None else {})}},
      {"$lookup":{"from":"attendance_logs","let":{"code":"$emp_code"},"pipeline":[{"$match":{"$expr":{"$and":[{"$eq":["$emp_code","$$code"]},{"$gte":["$date",start.isoformat()]},{"$lte":["$date",end.isoformat()]}]}}}],"as":"logs"}},
      {"$unwind":{"path":"$logs","preserveNullAndEmptyArrays":True}},
      {"$set":{"_d":{"$dateFromString":{"dateString":{"$ifNull":["$logs.date",start.isoformat()]},"timezone":"Asia/Kolkata"}},"_status":"$logs.status","_half":{"$ifNull":["$logs.half_day",False]},"_wh":"$logs.work_hours","_late":{"$ifNull":["$logs.late_minutes",0]},"_ot":{"$ifNull":["$logs.overtime_minutes",0]}}},
      {"$group":{"_id":{"department":"$department","emp":"$emp_code"},"headcount":{"$first":1},
       "present_days":{"$sum":{"$cond":[{"$and":[{"$in":["$_status",list(PRESENCE)]},{"$lt":[{"$dayOfWeek":{"date":"$_d","timezone":"Asia/Kolkata"}},7]},{"$ne":[{"$dayOfWeek":{"date":"$_d","timezone":"Asia/Kolkata"}},1]}]},{"$cond":["$_half",0.5,1]},0]}},
       "work_sum":{"$sum":{"$cond":[{"$and":[{"$in":["$_status",list(PRESENCE)]},{"$ne":["$_wh",None]}]},"$_wh",0]}},
       "work_count":{"$sum":{"$cond":[{"$and":[{"$in":["$_status",list(PRESENCE)]},{"$ne":["$_wh",None]}]},1,0]}},
       "late_count":{"$sum":{"$cond":[{"$gt":["$_late",0]},1,0]}},"total_late_minutes":{"$sum":"$_late"},
       "leave_count":{"$sum":{"$cond":[{"$eq":["$_status","LEAVE"]},1,0]}},"on_duty_count":{"$sum":{"$cond":[{"$eq":["$_status","ON_DUTY"]},1,0]}}}},
      {"$group":{"_id":"$_id.department","headcount":{"$sum":"$headcount"},"present_days":{"$sum":"$present_days"},"work_sum":{"$sum":"$work_sum"},"work_count":{"$sum":"$work_count"},"late_count":{"$sum":"$late_count"},"total_late_minutes":{"$sum":"$total_late_minutes"},"leave_count":{"$sum":"$leave_count"},"on_duty_count":{"$sum":"$on_duty_count"}}},
      {"$match":{"headcount":{"$gt":0}}},{"$sort":{"_id":1}},
      {"$project":{"_id":0,"department":"$_id","headcount":1,"present_days":{"$round":["$present_days",2]},"avg_work_hours":{"$cond":[{"$gt":["$work_count",0]},{"$divide":[{"$floor":{"$add":[{"$multiply":[{"$divide":["$work_sum","$work_count"]},100]},0.5]}},100]},None]},"late_count":1,"total_late_minutes":1,"leave_count":1,"on_duty_count":1}}]

@app.get("/analytics/departments/summary")
def department_summary(month:str=Query(...,pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),department:Optional[str]=None):
    items=list(db.employees.aggregate(department_summary_pipeline(month, department)))
    return {"month":month,"items":items}

def late_leaderboard_pipeline(month, limit=10, department=None):
    start, end = parse_month(month)
    pipeline=[{"$match":{"date":{"$gte":start.isoformat(),"$lte":end.isoformat()},"late_minutes":{"$gt":0}}},
      {"$group":{"_id":"$emp_code","total_late_minutes":{"$sum":"$late_minutes"},"late_count":{"$sum":1}}},
      {"$lookup":{"from":"employees","localField":"_id","foreignField":"emp_code","as":"employee"}}, {"$unwind":"$employee"}]
    if department is not None:
        pipeline.append({"$match":{"employee.department":department}})
    pipeline += [{"$setWindowFields":{"sortBy":{"total_late_minutes":-1},"output":{"rank":{"$rank":{}}}}},
      {"$match":{"rank":{"$lte":limit}}},{"$sort":{"total_late_minutes":-1,"_id":1}},
      {"$project":{"_id":0,"rank":1,"emp_code":"$_id","name":"$employee.name","department":"$employee.department","total_late_minutes":1,"late_count":1}}]
    return pipeline

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month:str=Query(...,pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),limit:int=Query(10,ge=1,le=50),department:Optional[str]=None):
    return {"month":month,"items":list(db.attendance_logs.aggregate(late_leaderboard_pipeline(month, limit, department)))}

def department_trend_pipeline(department, from_date, to_date):
    return [
        {"$documents": [{"start": from_date, "end": to_date, "department": department}]},
        {"$set": {"days": {"$range": [0, {"$add": [{"$dateDiff": {
            "startDate": {"$dateFromString": {"dateString": "$start", "timezone": "Asia/Kolkata"}},
            "endDate": {"$dateFromString": {"dateString": "$end", "timezone": "Asia/Kolkata"}}, "unit": "day"}}, 1]}]}}},
        {"$unwind": "$days"},
        {"$set": {"date": {"$dateToString": {"date": {"$dateAdd": {
            "startDate": {"$dateFromString": {"dateString": "$start", "timezone": "Asia/Kolkata"}},
            "unit": "day", "amount": "$days"}}, "format": "%Y-%m-%d", "timezone": "Asia/Kolkata"}}}},
        {"$lookup": {"from": "employees", "let": {"dep": "$department", "d": "$date"}, "pipeline": [
            {"$match": {"$expr": {"$and": [{"$eq": ["$department", "$$dep"]}, {"$lte": ["$joined_on", "$$d"]}]}}},
            {"$count": "n"}], "as": "hc"}},
        {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
                   "is_working_day": {"$and": [{"$gt": [{"$dayOfWeek": {"date": {"$dateFromString": {"dateString": "$date", "timezone": "Asia/Kolkata"}}, "timezone": "Asia/Kolkata"}}, 1]},
                                                {"$lt": [{"$dayOfWeek": {"date": {"$dateFromString": {"dateString": "$date", "timezone": "Asia/Kolkata"}}, "timezone": "Asia/Kolkata"}}, 7]}]}}},
        {"$lookup": {"from": "attendance_logs", "let": {"dep": "$department", "d": "$date"}, "pipeline": [
            {"$match": {"$expr": {"$eq": ["$date", "$$d"]}}},
            {"$lookup": {"from": "employees", "localField": "emp_code", "foreignField": "emp_code", "as": "e"}},
            {"$unwind": "$e"}, {"$match": {"$expr": {"$eq": ["$e.department", "$$dep"]}}},
            {"$group": {"_id": None,
                "present_count": {"$sum": {"$cond": [{"$in": ["$status", list(PRESENCE)]}, {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}},
                "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}}
        ], "as": "daily"}},
        {"$set": {"present_count": {"$ifNull": [{"$arrayElemAt": ["$daily.present_count", 0]}, 0]},
                   "late_count": {"$ifNull": [{"$arrayElemAt": ["$daily.late_count", 0]}, 0]}}},
        {"$set": {"attendance_rate": {"$cond": [{"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                                                    {"$divide": [{"$floor": {"$add": [{"$multiply": [{"$divide": ["$present_count", "$headcount"]}, 10000]}, 0.5]}}, 10000]}, None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"date": 1}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
                       "present_count": {"$round": ["$present_count", 2]}, "late_count": 1,
                       "attendance_rate": 1, "moving_avg_7d": {"$divide": [{"$floor": {"$add": [{"$multiply": ["$moving_avg_7d", 10000]}, 0.5]}}, 10000]}}}
    ]

@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to_date: str = Query(..., alias="to")):
    first, last = iso_date(from_date), iso_date(to_date)
    if last < first:
        raise HTTPException(422, "to must be on or after from")
    if (last - first).days + 1 > 92:
        raise HTTPException(422, "date range must not exceed 92 days")
    if not db.employees.find_one({"department": department}):
        raise HTTPException(404, "department not found")
    try:
        items = list(db.command({"aggregate": 1, "pipeline": department_trend_pipeline(department, from_date, to_date), "cursor": {}})["cursor"]["firstBatch"])
    except HTTPException:
        raise
    except Exception as exc:
        # Keep the message useful during local development without mislabeling
        # this as an explain failure: this is the department-trend query.
        raise HTTPException(
            status_code=500,
            detail=f"Department trend aggregation failed: {str(exc)}"
        ) from exc
    return {"department": department, "items": items}

@app.get("/admin/explain/{endpoint}")
def explain(endpoint:str,emp_code:Optional[str]=None,month:Optional[str]=None,department:Optional[str]=None,limit:int=Query(10,ge=1,le=50),date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[str]=None,from_date:Optional[str]=Query(None,alias="from"),to_date:Optional[str]=Query(None,alias="to"),page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    allowed={"attendance_list","employee_monthly","department_summary","late_leaderboard","department_trend"}
    if endpoint not in allowed: raise HTTPException(422,"unsupported endpoint")
    if endpoint=="attendance_list":
        if date_from: iso_date(date_from)
        if date_to: iso_date(date_to)
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be <= date_to")
        if status is not None and status not in STATUSES:
            raise HTTPException(422, "invalid status")
        q={}
        if emp_code is not None:q["emp_code"]=emp_code
        if status is not None:q["status"]=status
        if date_from or date_to:
            q["date"]={}
            if date_from:q["date"]["$gte"]=date_from
            if date_to:q["date"]["$lte"]=date_to
        op={"find":"attendance_logs","filter":q,"sort":{"date":-1,"emp_code":1},"skip":(page-1)*page_size,"limit":page_size}
        collection="attendance_logs"
    else:
        if not month and endpoint in {"employee_monthly","department_summary","late_leaderboard"}: raise HTTPException(422,"month is required")
        if month: parse_month(month)
        if endpoint=="employee_monthly":
            if not emp_code: raise HTTPException(422,"emp_code is required")
            op={"aggregate":"attendance_logs","pipeline":monthly_pipeline(emp_code,month),"cursor":{}};collection="attendance_logs"
        elif endpoint=="department_summary":
            op={"aggregate":"employees","pipeline":department_summary_pipeline(month, department),"cursor":{}};collection="employees"
        elif endpoint=="late_leaderboard":
            op={"aggregate":"attendance_logs","pipeline":late_leaderboard_pipeline(month, limit, department),"cursor":{}};collection="attendance_logs"
        else:
            if not department or not from_date or not to_date: raise HTTPException(422,"department, from and to are required")
            first, last = iso_date(from_date), iso_date(to_date)
            if last < first: raise HTTPException(422, "to must be on or after from")
            if (last-first).days+1 > 92: raise HTTPException(422, "date range must not exceed 92 days")
            if not db.employees.find_one({"department":department}):
                raise HTTPException(404, "department not found")
            op={"aggregate":1,"pipeline":department_trend_pipeline(department, from_date, to_date),"cursor":{}};collection="attendance_logs"
    try:
        explain_doc=db.command({"explain":op,"verbosity":"executionStats"})
        return {"endpoint":endpoint,"collection":collection,"explain":json_safe(explain_doc)}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not explain requested operation: {str(exc)}"
        ) from exc
