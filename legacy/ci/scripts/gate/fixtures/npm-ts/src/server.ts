import { app } from "./app.js";
app.listen(Number(process.env.PORT ?? 8080), "0.0.0.0");
