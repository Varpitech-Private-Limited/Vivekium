"use client";

// The PRISM Report modal (spec doc 4): opened from the PRISM Report button in
// the job page's candidate table.
//
// PRISM Report is the DOCUMENT. Tatva Assessment is the PROCESS that produces
// it. The two names are never used for each other.
//
// The file, the component and the route still say "ppi". That is deliberate and
// is not a leftover: the route is quoted in already-issued links, the module
// name appears in traces a rolling deploy is still writing, and reports written
// before today were filed under it. Renaming the symbols would make an existing
// report unreachable to buy nothing a reader ever sees.
//
// The report is IMMUTABLE: there is no edit control and no delete control
// anywhere in this component, and the backend answers PATCH/PUT/DELETE on the
// report route with 403. The UI is not the enforcement, it just never offers
// something the server would refuse.
//
// Section order is fixed (spec doc 4, part 3) and lives in ONE place,
// `REPORT_SECTION_ORDER` in components/functional-skills-report.
//
// THE DOWNLOAD HAS TWO GATES AND BOTH ARE THE SERVER'S. The candidate's
// retention consent (`report_download_allowed`) and gate G4
// (`pdf_available`): a report routed to a person is not downloaded until
// somebody has recorded a decision on it. When G4 holds it, the server's own
// sentence (`pdf_blocked_reason`) is shown instead of a button the route
// would refuse with 409. That notice is not a permission message and does
// not go through `permission-notice`: nobody lacks a grant, a decision is
// simply owed.
//
// CITATIONS ARE FETCHED ON DEMAND, AT MOST ONCE PER OPEN. Every read of them
// is an audited read of the candidate's answers, so opening the report must
// not record one; the first remark a reader expands does.

import * as React from "react";
import dynamic from "next/dynamic";
import { Download, Loader2, Lock } from "lucide-react";

import { apiFetch, apiGet } from "@/lib/api";
import { useToast } from "@/components/ui/toast";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import type { FunctionalReport } from "@/components/functional-skills-report";
import type { ReportCitations } from "@/components/report-citations";
import { AssessmentVideoSection } from "@/components/assessment-video-section";
import { Button } from "@/components/ui/button";

// Recharts is sizeable and only needed after a recruiter opens a report.
const FunctionalSkillsReportView = dynamic(
  () =>
    import("@/components/functional-skills-report").then(
      (module) => module.FunctionalSkillsReportView
    ),
  { loading: () => <div className="h-64 animate-pulse rounded-lg bg-muted" /> }
);

export const VIEW_ONLY_NOTICE =
  "The candidate chose view-only access to this assessment, so the PDF cannot be downloaded. You can read the full report here.";
export const G4_FALLBACK_NOTICE =
  "A person with integrity review authority must record a decision on this candidate before the PRISM Report can be downloaded.";

/** The server's sentence for a refused download, else a plain one. */
async function refusalSentence(res: Response): Promise<string> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    if (typeof body.detail === "string" && body.detail) return body.detail;
  } catch (e) {
    if (!(e instanceof SyntaxError)) throw e;
  }
  return `The PDF could not be downloaded (status ${res.status}). Please try again.`;
}

function filenameFromDisposition(header: string | null): string {
  const match = header?.match(/filename="([^"]+)"/);
  return match ? match[1] : "prism-report.pdf";
}

export function PPIReportModal({
  open,
  onOpenChange,
  linkId,
  candidateName,
  jobTitle,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  linkId: string | null;
  candidateName: string;
  jobTitle?: string | null;
}) {
  const { toast } = useToast();
  const [report, setReport] = React.useState<FunctionalReport | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  // One request per open report, shared by every remark: the promise is kept
  // so a second click (or a second remark) reuses the first fetch.
  const citationsRequest = React.useRef<Promise<ReportCitations> | null>(null);
  const [downloading, setDownloading] = React.useState(false);
  // The PDF is fetched, not navigated to: a navigation to a refusal (consent,
  // G4, an expired session) would save the error body as a ".pdf" file or show
  // the browser's generic failure. Here the server's own sentence is shown.
  const downloadPdf = async () => {
    if (!linkId) return;
    setDownloading(true);
    try {
      const res = await apiFetch(`/api/v2/assessments/reports/links/${linkId}/pdf`);
      if (!res.ok) throw new Error(await refusalSentence(res));
      const blob = await res.blob();
      const href = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = href;
      anchor.download = filenameFromDisposition(res.headers.get("Content-Disposition"));
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(href);
    } catch (e) {
      toast({
        title: "PDF not downloaded",
        description: e instanceof Error ? e.message : "The PDF could not be downloaded. Please try again.",
        variant: "destructive",
      });
    } finally {
      setDownloading(false);
    }
  };

  React.useEffect(() => {
    citationsRequest.current = null;
  }, [open, linkId]);
  const loadCitations = React.useCallback(() => {
    if (!linkId) return Promise.reject(new Error("No report is open."));
    if (!citationsRequest.current) {
      const request = apiGet<ReportCitations>(
        `/api/v2/assessments/reports/links/${linkId}/citations`
      );
      // A failed fetch is not cached, so the next click can try again.
      request.catch(() => {
        if (citationsRequest.current === request) citationsRequest.current = null;
      });
      citationsRequest.current = request;
    }
    return citationsRequest.current;
  }, [linkId]);

  React.useEffect(() => {
    if (!open || !linkId) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setReport(null);
    // Absolute /api/ path: the fetch wrapper routes these past API_BASE
    // (which is pinned to /api/v1) to the origin, so a v2 route is reachable.
    apiGet<FunctionalReport>(`/api/v2/assessments/reports/links/${linkId}`)
      .then((res) => {
        if (!cancelled) setReport(res);
      })
      .catch((e) => {
        if (cancelled) return;
        const message = e instanceof Error ? e.message : "Couldn't load the report";
        setError(message);
        toast({ title: "Report unavailable", description: message, variant: "destructive" });
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [open, linkId, toast]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90vh] max-w-5xl overflow-y-auto">
        <DialogHeader>
          <div className="flex flex-wrap items-start justify-between gap-3 pr-8">
            <div>
              <DialogTitle>PRISM Report</DialogTitle>
              <p className="text-sm">Evidence-Based Role Intelligence &amp; Suitability Mapping</p>
              <p className="mt-1 text-sm font-medium">
                {candidateName}
                {jobTitle ? ` (${jobTitle})` : ""}
              </p>
              {report?.reference_code ? (
                // Monospace and select-all so the code on a printed report and
                // the code in the candidate table can be compared character by
                // character, and copied without catching the words around it.
                // It identifies a row; it authorises nothing.
                <p className="mt-1 select-all type-ref">
                  {report.reference_code}
                </p>
              ) : null}
            </div>
            {linkId && report ? (
              report.report_download_allowed === false ? (
                // No dead button: the candidate chose view-only retention, so
                // the server answers 403 to the PDF route. Say why instead.
                <p
                  className="flex max-w-xs items-start gap-1.5 text-xs font-medium"
                  data-testid="pdf-view-only"
                >
                  <Lock className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
                  {VIEW_ONLY_NOTICE}
                </p>
              ) : report.pdf_available === false ? (
                // Gate G4: the server's own sentence, verbatim.
                <p className="max-w-xs text-xs font-medium" data-testid="pdf-g4-notice">
                  {report.pdf_blocked_reason || G4_FALLBACK_NOTICE}
                </p>
              ) : (
                <Button size="sm" variant="outline" disabled={downloading} onClick={() => void downloadPdf()}>
                  {downloading ? (
                    <Loader2 className="mr-2 h-4 w-4 animate-spin" aria-hidden />
                  ) : (
                    <Download className="mr-2 h-4 w-4" aria-hidden />
                  )}
                  Download PDF
                </Button>
              )
            ) : null}
          </div>
          <DialogDescription className="flex items-center gap-1.5">
            <Lock className="h-3.5 w-3.5" aria-hidden />
            This report is a permanent record and cannot be edited or deleted.
          </DialogDescription>
        </DialogHeader>

        {loading ? (
          <div className="flex items-center justify-center py-16">
            <Loader2 className="h-6 w-6 animate-spin" />
          </div>
        ) : error ? (
          <p className="py-10 text-center text-sm">{error}</p>
        ) : report ? (
          <FunctionalSkillsReportView report={report} loadCitations={loadCitations} />
        ) : null}

        {/* Assessment video (2026-09-05 dashboard/video spec section 20).
            BESIDE the report, never inside it: the PRISM Report's section
            order is fixed and test-pinned, and the video is a separate,
            consented artifact whose availability changes over time. Rendered
            from its own live metadata route even while the report itself
            failed to load, because the video question is independent. */}
        {!loading && linkId ? <AssessmentVideoSection linkId={linkId} /> : null}
      </DialogContent>
    </Dialog>
  );
}
