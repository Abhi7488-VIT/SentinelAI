// Container healthcheck: proves the Next.js server is up and serving requests.
export function GET(): Response {
  return Response.json({ status: "ok" });
}
