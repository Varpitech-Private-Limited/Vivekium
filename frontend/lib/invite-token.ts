type Param = string | string[] | undefined;

// A repeated query key arrives as string[]; the first non-empty value wins.
// `invite_code` is the legacy key.
export function inviteToken(params: { invite?: Param; invite_code?: Param }): string {
  const first = (value: Param) => (Array.isArray(value) ? value[0] : value)?.trim() ?? "";
  return first(params.invite) || first(params.invite_code);
}
