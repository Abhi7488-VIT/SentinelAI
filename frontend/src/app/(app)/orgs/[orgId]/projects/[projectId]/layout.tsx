import { ProjectFrame } from "@/components/project/project-frame";

export default function ProjectLayout({ children }: LayoutProps<"/orgs/[orgId]/projects/[projectId]">) {
  return <ProjectFrame>{children}</ProjectFrame>;
}
