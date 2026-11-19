export interface ReportPeriod {
  range?: { from: Date; to: Date };
}

export interface UsageReportJob {
  report_id: string;
  status: "PENDING" | "STARTED" | "FAILURE" | "SUCCESS";
  time_created: string;
}

// A finished usage report as `GET /admin/usage-report` lists it.
export interface UsageReport {
  report_name: string;
  requestor: string | null;
  time_created: string;
  period_from: string | null;
  period_to: string | null;
}
