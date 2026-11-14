import "./styles.css";
import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { setUnauthorizedHandler } from "@/lib/api";
import { makeQueryClient } from "@/lib/queries";
import { makeRouter } from "./router";

const queryClient = makeQueryClient();
const router = makeRouter(queryClient);

// Without a session, the sign-in page, and back here afterwards.
setUnauthorizedHandler(() => {
  queryClient.clear();
  const next = router.state.location.href;
  if (router.state.location.pathname !== "/signin") {
    router.navigate({ to: "/signin", search: { next: next === "/" ? undefined : next } });
  }
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
