import { message } from "../../lib/message";
export function GET() { return Response.json({ status: message() }); }
