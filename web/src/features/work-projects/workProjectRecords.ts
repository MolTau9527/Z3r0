import {
  getWorkProject,
  getWorkProjectGraph,
  queryWorkProjectAssets,
  queryWorkProjectFindings,
} from "../../shared/api/workProjects";
import type {
  WorkProject,
  WorkProjectAsset,
  WorkProjectFinding,
  WorkProjectGraphSnapshot,
} from "../../shared/api/types";

export type ProjectRecordTab = "assets" | "findings" | "attack-paths" | "graph";

export type WorkProjectRecords = {
  assets: WorkProjectAsset[];
  findings: WorkProjectFinding[];
  graph: WorkProjectGraphSnapshot;
};

export type WorkProjectRecordSnapshot = {
  project: WorkProject | null;
  records: WorkProjectRecords;
};

export const EMPTY_WORK_PROJECT_GRAPH: WorkProjectGraphSnapshot = {
  edges: [],
  attack_paths: [],
  attack_path_steps: [],
};

export const EMPTY_WORK_PROJECT_RECORDS: WorkProjectRecords = {
  assets: [],
  findings: [],
  graph: EMPTY_WORK_PROJECT_GRAPH,
};

export async function loadWorkProjectRecordSnapshot(projectId: number): Promise<WorkProjectRecordSnapshot> {
  const [projectResponse, assetsResponse, findingsResponse, graphResponse] = await Promise.all([
    getWorkProject(projectId),
    queryWorkProjectAssets(projectId, { page: 1, size: 100, keyword: "" }),
    queryWorkProjectFindings(projectId, { page: 1, size: 100, keyword: "" }),
    getWorkProjectGraph(projectId),
  ]);

  return {
    project: projectResponse.data ?? null,
    records: {
      assets: assetsResponse.data?.items ?? [],
      findings: findingsResponse.data?.items ?? [],
      graph: graphResponse.data ?? EMPTY_WORK_PROJECT_GRAPH,
    },
  };
}
