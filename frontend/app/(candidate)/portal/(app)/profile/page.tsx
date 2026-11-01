// My Profile, the candidate's single, unified profile (client decision,
// 2026-07-27). It replaced the old "Settings" page and holds everything a
// candidate maintains about themselves in one place:
//
//   * account details and, for password accounts, the password;
//   * the MAIN resume, uploadable and re-uploadable at any time;
//   * their employment history and its background verification (there is no
//     document upload here; the readiness answer lives in the application
//     form);
//   * the profile form, answered once here instead of per application;
//   * their consents: keeping the profile, retention choices, and the full
//     record of what they agreed to and when;
//   * the appearance toggle (the only place it lives, claude.md rule 10).
//
// Role is deliberately not shown: a candidate has exactly one.
//
// Order is by how often a card is used, and the destructive card is last.

import { SettingsPage } from "@/components/settings-page";
import { BackgroundVerificationCard } from "@/components/background-verification-card";
import { ConsentHistoryCard } from "@/components/consent-history-card";
import { ConsentRenewalCard } from "@/components/consent-renewal-card";
import { EmploymentHistoryCard } from "@/components/employment-history-card";
import { CandidateProfileForm } from "@/components/candidate-profile-form";
import { DataRetentionCard } from "@/components/data-retention-card";
import { DeleteProfileCard } from "@/components/delete-profile-card";
import { MainResumeCard } from "@/components/main-resume-card";
import { ProjectsSection } from "@/components/projects-section";

export const metadata = { title: "My Profile" };

export default function CandidateProfilePage() {
  return (
    <SettingsPage
      title="My Profile"
      description="Your details, your main resume, and the answers reused on every application."
      showRole={false}
    >
      <MainResumeCard />
      <EmploymentHistoryCard />
      <BackgroundVerificationCard />
      <ProjectsSection />
      <CandidateProfileForm />
      <ConsentRenewalCard />
      <DataRetentionCard />
      <ConsentHistoryCard />
      {/* LAST, and that is the whole placement argument: a destructive
          control above the things it destroys invites a mis-click from
          somebody who came here to edit their resume. */}
      <DeleteProfileCard />
    </SettingsPage>
  );
}
