import "./styles.css";
import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import { QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ConfirmProvider, Toaster } from "@/components/ui/overlays";
import { setUnauthorizedHandler } from "@/lib/api";
import { makeQueryClient } from "@/lib/queries";
import { makeRouter } from "./router";

const queryClient = makeQueryClient();
const router = makeRouter(queryClient);

// A session that expires mid-use sends the admin back to the login page.
setUnauthorizedHandler(() => {
  queryClient.clear();
  const next = router.state.location.href;
  router.navigate({ to: "/login", search: { next: next === "/" ? undefined : next } });
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <TooltipPrimitive.Provider>
        <ConfirmProvider>
          <RouterProvider router={router} />
          <Toaster />
        </ConfirmProvider>
      </TooltipPrimitive.Provider>
    </QueryClientProvider>
  </StrictMode>,
);
