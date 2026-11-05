import { Link } from "@tanstack/react-router";
import { Icon } from "@/components/ui/icons";
import { EmptyState } from "@/components/ui/primitives";
import { buttonClass } from "@/lib/buttonStyles";

export function NotFound() {
  return (
    <div className="flex min-h-[60vh] items-center justify-center">
      <EmptyState
        icon="search"
        title="Nothing here"
        action={<Link to="/" className={buttonClass("secondary")}><Icon name="overview" size={15} />Back to the overview</Link>}
      >
        This page does not exist, or the thing it showed was deleted.
      </EmptyState>
    </div>
  );
}
