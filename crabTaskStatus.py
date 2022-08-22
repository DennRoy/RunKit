import re
from enum import Enum

class Status(Enum):
  Unknown = 0
  Bootstrapped = 1
  InProgress = 2
  Finished = 3

class StatusOnServer(Enum):
  SUBMITTED = 1

class StatusOnScheduler(Enum):
  SUBMITTED = 1
  FAILED = 2

class CrabWarningCategory(Enum):
  Unknown = 0
  BlocksSkipped = 1
  ShortRuntime = 2
  LowCpuEfficiency = 3

class JobStatus(Enum):
  unsubmitted = 0
  idle = 1
  running = 2
  toRetry = 3
  finished = 4
  failed = 5
  transferring = 6

class CrabWarning:
  known_warnings = {
    r"Some blocks from dataset '.+' were skipped  because they are only present at blacklisted and/or not-whitelisted sites.": CrabWarningCategory.BlocksSkipped,
    r"the max jobs runtime is less than 30% of the task requested value": CrabWarningCategory.ShortRuntime,
    r"the average jobs CPU efficiency is less than 50%": CrabWarningCategory.LowCpuEfficiency,
  }
  def __init__(self, warning_text):
    self.category = CrabWarningCategory.Unknown
    self.warning_text = warning_text
    for known_warning, category in CrabWarning.known_warnings.items():
      if re.match(known_warning, warning_text):
        self.category = category
        break

class LogEntryParser:
  @staticmethod
  def Parse(log_lines):
    task_status = CrabTaskStatus()
    n = 0
    N = len(log_lines)
    try:
      while n < N:
        if len(log_lines[n].strip()) == 0:
          n += 1
          continue
        method_found = False
        for parse_key, parse_method in LogEntryParser._parser_dict.items():
          if log_lines[n].startswith(parse_key):
            value = log_lines[n][len(parse_key):].strip()
            method_found = True
            if type(parse_method) == str:
              setattr(task_status, parse_method, value)
              n += 1
            elif parse_method is not None:
              n = parse_method(task_status, log_lines, n, value)
            else:
              n += 1
            break
        if not method_found:
          raise RuntimeError(f'Unknown log line {n} = "{log_lines[n]}".')
      if task_status.status_on_server == StatusOnServer.SUBMITTED:
        task_status.status = Status.InProgress
    except RuntimeError as e:
      task_status.status = Status.Unknown
      task_status.parse_error = str(e)
    return task_status

  def sched_worker(task_status, log_lines, n, value):
    match = re.match(r'(.*) - (.*)', value)
    if match is None:
      raise RuntimeError("Invalid Grid scheduler - Task Worker")
    task_status.grid_scheduler = match.group(1)
    task_status.task_worker = match.group(2)
    return n + 1

  def status_on_server(task_status, log_lines, n, value):
    if value not in StatusOnServer.__members__:
      raise RuntimeError(f'Unknown status on the CRAB server = "{value}"')
    task_status.status_on_server = StatusOnServer[value]
    return n + 1

  def status_on_scheduler(task_status, log_lines, n, value):
    if value not in StatusOnScheduler.__members__:
      raise RuntimeError(f'Unknown status on the scheduler = "{value}"')
    task_status.status_on_scheduler = StatusOnScheduler[value]
    return n + 1

  def warning(task_status, log_lines, n, value):
    warning_text = value
    while n < len(log_lines) - 1:
      n += 1
      line = log_lines[n].strip()
      if len(line) == 0 or log_lines[n][0] != ' ':
        break
      warning_text += f'\n{log_lines[n].strip()}'
    task_status.warnings.append(CrabWarning(warning_text))
    return n

  def job_status(task_status, log_lines, n, value):
    job_stat_strs = [ value ]
    n += 1
    while n < len(log_lines):
      line = log_lines[n].strip()
      if len(line) == 0: break
      job_stat_strs.append(line)
      n += 1
    for s in job_stat_strs:
      m = re.match(r'^([^ ]+) *([0-9\.]+)% *\( *([0-9]+)/([0-9]+)\)', s)
      if m is None:
        raise RuntimeError(f'can not extract job status from "{s}"')
      job_status_str = m.group(1)
      if job_status_str not in JobStatus.__members__:
        raise RuntimeError(f'Unknown job status = {job_status_str}')
      status = JobStatus[job_status_str]
      if status in task_status.job_stat:
        raise RuntimeError(f'Duplicated job status information for {status.name}')
      try:
        n_jobs = int(m.group(3))
        n_total = int(m.group(4))
      except ValueError:
        raise RuntimeError(f'Number of jobs is not an integer. "{s}"')
      task_status.job_stat[status] = n_jobs
      #fraction = float(m.group{2})

      if task_status.n_jobs_total is None:
        task_status.n_jobs_total = n_total
      if task_status.n_jobs_total != n_total:
        raise RuntimeError("Inconsistent total number of jobs")
    return n

  def error_summary(task_status, log_lines, n, value):
    error_stat_strs = []
    end_found = False
    while n < len(log_lines):
      n += 1
      line = log_lines[n].strip()
      if len(line) == 0:
        continue
      if line == LogEntryParser.error_summary_end:
        end_found = True
        break
    if not end_found:
      raise RuntimeError("Unable to find the end of the error summary")
    for stat_str in error_stat_strs:
      stat_str = stat_str.strip()
      match = re.match(r'([0-9]+) jobs failed with exit code ([0-9]+)', stat_str)
      if match is None:
        match = re.match(r'Could not find exit code details for [0-9]+ jobs.', stat_str)
        if match is None:
          raise RuntimeError(f'Unknown job summary string = "{stat_str}"')
        task_status.error_stat["Unknown"] = int(match.group(1))
      else:
        task_status.error_stat[int(match.group(2))] = int(match.group(1))
    return n + 1

  def run_summary(task_status, log_lines, n, value):
    if n + 4 >= len(log_lines):
      raise RuntimeError("Incomplete summary of run jobs")

    mem_str = log_lines[n + 1].strip()
    match = re.match(r'^\* Memory: ([0-9]+)MB min, ([0-9]+)MB max, ([0-9]+)MB ave$', mem_str)
    if match is None:
      raise RuntimeError(f'Invalid memory stat = "{mem_str}"')
    task_status.run_stat["Memory"] = {
      'min': int(match.group(1)),
      'max': int(match.group(2)),
      'ave': int(match.group(3)),
    }

    def to_seconds(hh_mm_ss):
      hh, mm, ss = [ int(s) for s in hh_mm_ss.split(':') ]
      return ((hh * 60) + mm) + ss

    runtime_str = log_lines[n + 2].strip()
    match = re.match(r'^\* Runtime: ([0-9]+:[0-9]+:[0-9]+) min, ([0-9]+:[0-9]+:[0-9]+) max, ([0-9]+:[0-9]+:[0-9]+) ave$',
                     runtime_str)
    if match is None:
      raise RuntimeError(f'Invalid runtime stat = "{runtime_str}"')
    task_status.run_stat["Runtime"] = {
      'min': to_seconds(match.group(1)),
      'max': to_seconds(match.group(2)),
      'ave': to_seconds(match.group(3)),
    }

    cpu_str = log_lines[n + 3].strip()
    match = re.match(r'^\* CPU eff: ([0-9]+)% min, ([0-9]+)% max, ([0-9]+)% ave$', cpu_str)
    if match is None:
      raise RuntimeError(f'Invalid CPU eff stat = "{cpu_str}"')
    task_status.run_stat["CPU"] = {
      'min': int(match.group(1)),
      'max': int(match.group(2)),
      'ave': int(match.group(3)),
    }

    waste_str = log_lines[n + 4].strip()
    match = re.match(r'^\* Waste: ([0-9]+:[0-9]+:[0-9]+) \(([0-9]+)% of total\)$', waste_str)
    if match is None:
      raise RuntimeError(f'Invalid waste stat = "{waste_str}"')
    task_status.run_stat["CPU"] = {
      'time': to_seconds(match.group(1)),
      'fraction_of_total': int(match.group(2)),
    }

    return n + 5

  def task_boostrapped(task_status, log_lines, n, value):
    if n + 1 >= len(log_lines) or log_lines[n + 1].strip() != LogEntryParser.status_will_be_available:
      raise RuntimeError("Unexpected bootstrap message")
    task_status.status = Status.Bootstrapped
    return n + 2

  _parser_dict = {
    "CRAB project directory:": "project_dir",
    "Task name:": "task_name",
    "Grid scheduler - Task Worker:": sched_worker,
    "Status on the CRAB server:": status_on_server,
    "Task URL to use for HELP:": "task_url",
    "Dashboard monitoring URL:": "dashboard_url",
    "Status on the scheduler:": status_on_scheduler,
    "Warning:": warning,
    "Jobs status:": job_status,
    "No publication information": None,
    "Error Summary:": error_summary,
    "Log file is": "crab_log_file",
    "Summary of run jobs:": run_summary,
    "Task bootstrapped": task_boostrapped,
  }
  error_summary_end = "Have a look at https://twiki.cern.ch/twiki/bin/viewauth/CMSPublic/JobExitCodes for a description of the exit codes."
  status_will_be_available = "Status information will be available within a few minutes"

class CrabTaskStatus:
  def __init__(self):
    self.status = Status.Unknown
    self.log_lines = None
    self.job_stat = {}
    self.n_jobs_total = None
    self.parse_error = None
    self.error_stat = {}
    self.warnings = []
    self.run_stat = {}

if __name__ == "__main__":
  import sys

  log_file = sys.argv[1]
  with open(log_file, 'r') as f:
    log_lines = f.readlines()
  jobStatus = LogEntryParser.Parse(log_lines)
  print(jobStatus.status)
  if jobStatus.status == Status.Unknown:
    print(jobStatus.parse_error)
  else:
    for status, n in jobStatus.job_stat.items():
      print(f'{status.name} {float(n)/jobStatus.n_jobs_total * 100:.1f}% ({n}/{jobStatus.n_jobs_total})')
  for warning in jobStatus.warnings:
    if warning.category == CrabWarningCategory.Unknown:
      print(f'Unknown warning\n-----\n{warning.warning_text}\n-----\n')
