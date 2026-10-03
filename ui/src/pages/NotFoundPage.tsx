import { Link } from "react-router";
import { PageHeading } from "../components/PageHeading";

export function NotFoundPage() {
  return (
    <>
      <PageHeading title="Page not found" />
      <p>This page does not exist in the MIAS dashboard.</p>
      <p>
        <Link to="/">Go to the Overview</Link>
      </p>
    </>
  );
}
