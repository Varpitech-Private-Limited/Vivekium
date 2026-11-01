"use client";

// The Assessment video section of the Executive Profile surface (2026-09-05
// dashboard/video spec, sections 15, 18, 20): duration, an honest status word,
// and the two delivery actions.
//
// ONE MODE, AND THE OLD ROWS SAY WHAT THEY ARE (Phase 3, 2026-09-24). Every
// assessment is now the same proctored session, so a row naming "the mode"
// told the hiring team nothing and is gone. A recording made before that, as
// a video interview, is still readable, and it says so in one sentence: its
// layout (one answer spoken per question, no typed answers) is not what a
// reviewer now expects, and a recording that looks unlike every other one
// with nothing explaining why reads as a broken recording.
//
// This section sits BESIDE the PRISM Report, never inside it. The report is an
// immutable document with a fixed, test-pinned section order; the video is a
// separate consented artifact whose availability changes over time, so it
// renders from its own live metadata route rather than from the stored report
// payload.
//
// Security posture, mirrored from the server (the UI is not the enforcement):
//   * The metadata GET carries no bucket name and no object key.
//   * A presigned URL is fetched only when the person clicks Preview or
//     Download, is used immediately, and is never stored anywhere.
//   * The Download button exists only when the server says the candidate
//     consented (`download_available`); otherwise the reason is shown instead
//     of a dead control.
//   * "Retry processing" appears only when the server says the recording is in
//     a retryable state; the retry route is gated on the same capability that
//     opened this profile, so a visible button is an honest one.

import * as React from "react";
import { Download, Loader2, Lock, Play, RotateCcw, Video } from "lucide-react";

import { apiGet, apiPost } from "@/lib/api";
import type { VideoAccess, VideoDelivery, VideoRecordingStatus } from "@/lib/types";
import { useToast } from "@/components/ui/toast";
import { Button } from "@/components/ui/button";

/** The stored mode a recording made before 2026-09-24 as a video interview
 *  still carries. Compared, never written: no new session takes it. */
export const LEGACY_VIDEO_INTERVIEW_MODE = "video_interview";

export const LEGACY_FORMAT_NOTE =
  "This assessment was taken as a video interview, an earlier format that is no " +
  "longer offered. The recording is shown as it was made.";

/** Stages in which the recording is still on its way to being playable. */
const IN_PROGRESS_STAGES = ["recording", "uploaded", "compressing", "storing"];
const VIDEO_POLL_MS = 15_000;

/** 1840 seconds reads "30:40"; anything over an hour reads "1:02:05". */
export function formatDuration(totalSeconds: number): string {
  const whole = Math.max(0, Math.round(totalSeconds));
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const seconds = whole % 60;
  const mm = hours > 0 ? String(minutes).padStart(2, "0") : String(minutes);
  const ss = String(seconds).padStart(2, "0");
  return hours > 0 ? `${hours}:${mm}:${ss}` : `${mm}:${ss}`;
}

export function AssessmentVideoSection({ linkId }: { linkId: string }) {
  const { toast } = useToast();
  const [access, setAccess] = React.useState<VideoAccess | null>(null);
  const [loading, setLoading] = React.useState(true);
  const [error, setError] = React.useState<string | null>(null);
  const [playerUrl, setPlayerUrl] = React.useState<string | null>(null);
  const [working, setWorking] = React.useState<"preview" | "download" | "retry" | null>(
    null
  );

  const load = React.useCallback(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    apiGet<VideoAccess>(`/videos/links/${linkId}`)
      .then((res) => {
        if (!cancelled) setAccess(res);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(
          e instanceof Error ? e.message : "Couldn't load the video details"
        );
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [linkId]);

  React.useEffect(() => {
    setAccess(null);
    setPlayerUrl(null);
    return load();
  }, [load]);

  // While the pipeline is still working the answer changes by itself: look
  // again, quietly, only while the tab is visible, and stop at any other stage.
  const inProgress = access !== null && IN_PROGRESS_STAGES.includes(access.video_stage);
  React.useEffect(() => {
    if (!inProgress) return;
    const timer = window.setInterval(() => {
      if (document.visibilityState !== "visible") return;
      apiGet<VideoAccess>(`/videos/links/${linkId}`)
        .then(setAccess)
        .catch(() => undefined);
    }, VIDEO_POLL_MS);
    return () => window.clearInterval(timer);
  }, [inProgress, linkId]);

  const openPreview = async () => {
    setWorking("preview");
    try {
      // The URL is minted on click, handed straight to the player and never
      // stored. It expires server-side; a fresh click mints a fresh one.
      const delivery = await apiPost<VideoDelivery>(
        `/videos/links/${linkId}/preview`
      );
      setPlayerUrl(delivery.url);
    } catch (e) {
      toast({
        title: "Preview unavailable",
        description:
          e instanceof Error ? e.message : "The video could not be opened.",
        variant: "destructive",
      });
    } finally {
      setWorking(null);
    }
  };

  const download = async () => {
    setWorking("download");
    try {
      const delivery = await apiPost<VideoDelivery>(
        `/videos/links/${linkId}/download`
      );
      // The URL carries an attachment disposition, so navigating to it saves
      // the file rather than leaving the page.
      const anchor = document.createElement("a");
      anchor.href = delivery.url;
      if (delivery.filename) anchor.download = delivery.filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
    } catch (e) {
      toast({
        title: "Download unavailable",
        description:
          e instanceof Error ? e.message : "The video could not be downloaded.",
        variant: "destructive",
      });
    } finally {
      setWorking(null);
    }
  };

  const retry = async () => {
    if (!access?.recording_id) return;
    setWorking("retry");
    try {
      // The assessments router is mounted under /api/v2 only, so the path
      // carries its own prefix (the same convention assessment-conversation.tsx
      // uses); a v1-relative "/assessments/..." would 404 in production, which
      // is exactly what lib/api-mount-parity.test.ts exists to catch.
      await apiPost<VideoRecordingStatus>(
        `/api/v2/assessments/videos/recordings/${access.recording_id}/retry`
      );
      toast({
        title: "Processing restarted",
        description:
          "The recording is being processed again. This can take a few minutes.",
      });
      load();
    } catch (e) {
      toast({
        title: "Retry failed",
        description:
          e instanceof Error ? e.message : "The recording could not be retried.",
        variant: "destructive",
      });
    } finally {
      setWorking(null);
    }
  };

  return (
    <section aria-label="Assessment video" className="rounded-lg border p-4">
      <h3 className="mb-3 flex items-center gap-2 text-heading">
        <Video className="h-4 w-4" aria-hidden />
        Assessment video
      </h3>
      {loading ? (
        <div className="flex items-center gap-2 py-4 text-sm">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
          Checking the recording
        </div>
      ) : error ? (
        <p className="py-2 text-sm">{error}</p>
      ) : access ? (
        <div className="space-y-3">
          {access.assessment_mode === LEGACY_VIDEO_INTERVIEW_MODE ? (
            <p className="text-sm">{LEGACY_FORMAT_NOTE}</p>
          ) : null}
          <dl className="grid gap-x-8 gap-y-1 text-sm sm:grid-cols-3">
            {access.duration_seconds !== null ? (
              <div>
                <dt className="font-medium">Duration</dt>
                <dd>{formatDuration(access.duration_seconds)}</dd>
              </div>
            ) : null}
            <div>
              <dt className="font-medium">Status</dt>
              {/* Teal marks evidence that is actually there to review. */}
              <dd
                className={
                  access.video_status === "Ready" ? "font-medium text-teal-700" : ""
                }
              >
                {access.video_status}
              </dd>
            </div>
          </dl>
          <p className="text-sm">{access.video_stage_detail || access.video_status_detail}</p>
          {access.last_reported_at ? (
            <p className="text-xs">
              Last updated {new Date(access.last_reported_at).toLocaleString()}
            </p>
          ) : null}

          {access.preview_available ? (
            <div className="flex flex-wrap items-center gap-2">
              {playerUrl === null ? (
                <Button
                  size="sm"
                  className="gap-1.5"
                  disabled={working !== null}
                  onClick={openPreview}
                >
                  {working === "preview" ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
                  ) : (
                    <Play className="h-3.5 w-3.5" aria-hidden />
                  )}
                  Preview video
                </Button>
              ) : null}
              {access.download_available ? (
                <Button
                  size="sm"
                  variant="outline"
                  className="gap-1.5"
                  disabled={working !== null}
                  onClick={download}
                >
                  {working === "download" ? (
                    <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
                  ) : (
                    <Download className="h-3.5 w-3.5" aria-hidden />
                  )}
                  Download video
                </Button>
              ) : access.download_blocked_reason ? (
                // No dead button: say why the download is withheld.
                <p className="flex items-center gap-1.5 text-xs font-medium">
                  <Lock className="h-3.5 w-3.5" aria-hidden />
                  {access.download_blocked_reason}
                </p>
              ) : null}
            </div>
          ) : null}

          {access.can_retry ? (
            <Button
              size="sm"
              variant="outline"
              className="gap-1.5"
              disabled={working !== null}
              onClick={retry}
            >
              {working === "retry" ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" aria-hidden />
              ) : (
                <RotateCcw className="h-3.5 w-3.5" aria-hidden />
              )}
              Retry processing
            </Button>
          ) : null}

          {playerUrl !== null ? (
            // Plain HTML5 playback: the stored object is an mp4 the browser
            // plays natively, and S3 serves the player's seek (range) requests
            // itself. Nothing else is loaded for this.
            <video
              controls
              autoPlay
              preload="metadata"
              className="aspect-video w-full rounded-lg border bg-navy-900"
              src={playerUrl}
              onError={() => {
                // The likeliest cause is an expired URL after a long pause.
                setPlayerUrl(null);
                toast({
                  title: "Playback stopped",
                  description:
                    "The viewing session expired. Use Preview video to start a new one.",
                });
              }}
            >
              Your browser cannot play this video here. Use the download
              option if it is offered.
            </video>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}
