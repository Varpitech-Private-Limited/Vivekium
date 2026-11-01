import type { Metadata } from "next";

import { JoinFlow } from "@/components/join-flow";
import { inviteToken } from "@/lib/invite-token";

// An invitation acceptance page is addressed by a token in the query string
// and is not content. `index: false` keeps it out of results; `follow` is left
// alone so the links out of the page still carry weight.
export const metadata: Metadata = {
  title: "Join your team",
  robots: { index: false },
};

// Next 16 makes `searchParams` a Promise. Read synchronously it is undefined,
// which showed "This invitation link is incomplete" for every valid link.
export default async function JoinPage({
  searchParams,
}: {
  searchParams: Promise<{
    invite?: string | string[];
    invite_code?: string | string[];
  }>;
}) {
  return <JoinFlow token={inviteToken(await searchParams)} />;
}
