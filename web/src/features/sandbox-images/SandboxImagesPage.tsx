import { Button, Popconfirm, Tag, Tooltip } from "@douyinfe/semi-ui";
import { Ban, Boxes, Fingerprint, RotateCcw, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { cancelSandboxImage, createSandboxImage, deleteSandboxImage, querySandboxImages, retrySandboxImage } from "../../shared/api/sandboxImages";
import type { CreateSandboxImageRequest, SandboxImage } from "../../shared/api/types";
import { ResourcePageShell } from "../../shared/components/ResourcePageShell";
import { ResourceTable, type ResourceColumn } from "../../shared/components/ResourceTable";
import { useAdminResourceHeader } from "../../shared/hooks/useAdminResourceHeader";
import { usePagedResourceList } from "../../shared/hooks/usePagedResourceList";
import { useResourceAction } from "../../shared/hooks/useResourceAction";
import { useResourceSubmit } from "../../shared/hooks/useResourceSubmit";
import { formatDateTime } from "../../shared/lib/date";
import { formatBytes } from "../../shared/lib/number";
import { SANDBOX_IMAGE_STATUS_COLOR, SANDBOX_IMAGE_STATUS_LABEL } from "../../shared/lib/labels";
import { SandboxImageFormModal } from "./SandboxImageFormModal";

export function SandboxImagesPage() {
  const {
    items: images, page, keyword, loading, loadItems: loadImages, total, rangeStart, rangeEnd,
    setKeyword, search, previous, next, canGoBack, canGoNext,
  } = usePagedResourceList<SandboxImage>({ query: querySandboxImages });
  const [modalOpen, setModalOpen] = useState(false);

  const { run: cancelImage, busyId: cancelingId } = useResourceAction<SandboxImage>(
    (image) => cancelSandboxImage(image.id), loadImages,
  );
  const { run: retryImage, busyId: retryingId } = useResourceAction<SandboxImage>(
    (image) => retrySandboxImage(image.id), loadImages,
  );
  const { run: deleteImage, busyId: deletingId } = useResourceAction<SandboxImage>(
    (image) => deleteSandboxImage(image.id), loadImages,
  );

  useAdminResourceHeader({
    createLabel: "Create Image",
    refreshLabel: "Refresh sandbox images",
    loading,
    onCreate: () => setModalOpen(true),
    onRefresh: loadImages,
  });

  const { saving, submit } = useResourceSubmit({
    onSuccess: async () => {
      setModalOpen(false);
      await loadImages();
    },
  });

  const summary = useMemo(
    () => images.reduce(
      (acc, image) => ({
        ready: acc.ready + (image.status === "ready" ? 1 : 0),
        pulling: acc.pulling + (image.status === "pulling" ? 1 : 0),
        canceled: acc.canceled + (image.status === "canceled" ? 1 : 0),
      }),
      { ready: 0, pulling: 0, canceled: 0 },
    ),
    [images],
  );

  const handleCreate = (payload: CreateSandboxImageRequest) => submit(() => createSandboxImage(payload));

  const columns: ResourceColumn<SandboxImage>[] = [
    {
      key: "image", header: "Image", width: "minmax(280px, 360px)",
      render: (image) => (
        <div className="image-identity">
          <div className="resource-avatar"><Boxes size={18} /></div>
          <div>
            <strong>{image.image_name}</strong>
            <span><Fingerprint size={13} />{renderImageHash(image.image_hash)}</span>
          </div>
        </div>
      ),
    },
    {
      key: "status", header: "Status", width: "110px",
      render: (image) => (
        <Tag color={SANDBOX_IMAGE_STATUS_COLOR[image.status]}>{SANDBOX_IMAGE_STATUS_LABEL[image.status]}</Tag>
      ),
    },
    { key: "size", header: "Size", width: "120px", render: (image) => formatBytes(image.image_size) },
    { key: "created", header: "Created", width: "minmax(150px, 1fr)", render: (i) => formatDateTime(i.created_at) },
    { key: "updated", header: "Updated", width: "minmax(150px, 1fr)", render: (i) => formatDateTime(i.updated_at) },
    {
      key: "actions", header: "Actions", width: "104px",
      render: (image) => (
        <div className="row-actions">
          <Button icon={<Ban size={15} />} theme="borderless"
            disabled={image.status !== "pulling"} loading={cancelingId === image.id}
            aria-label={`Cancel ${image.image_name}`} onClick={() => void cancelImage(image)}
          />
          <Button icon={<RotateCcw size={15} />} theme="borderless"
            disabled={image.status !== "failed" && image.status !== "canceled"}
            loading={retryingId === image.id}
            aria-label={`Retry ${image.image_name}`} onClick={() => void retryImage(image)}
          />
          <Popconfirm title="Delete image" content={`Delete ${image.image_name}?`} okType="danger" onConfirm={() => void deleteImage(image)}>
            <Button icon={<Trash2 size={15} />} theme="borderless" type="danger"
              loading={deletingId === image.id} aria-label={`Delete ${image.image_name}`}
            />
          </Popconfirm>
        </div>
      ),
    },
  ];

  return (
    <>
      <ResourcePageShell
        searchPlaceholder="Search image name, hash, or status"
        keyword={keyword}
        loading={loading}
        metrics={[
          { label: "Total", value: total },
          { label: "Ready", value: summary.ready },
          { label: "Pulling", value: summary.pulling },
          { label: "Canceled", value: summary.canceled },
        ]}
        empty={images.length === 0}
        emptyIcon={<Boxes size={42} />}
        emptyTitle="No images found"
        page={page}
        rangeStart={rangeStart}
        rangeEnd={rangeEnd}
        total={total}
        canGoBack={canGoBack}
        canGoNext={canGoNext}
        onKeywordChange={setKeyword}
        onSearch={search}
        onPrevious={previous}
        onNext={next}
      >
        <ResourceTable<SandboxImage>
          ariaLabel="Sandbox images"
          columns={columns}
          rows={images}
          rowKey={(image) => image.id}
        />
      </ResourcePageShell>

      <SandboxImageFormModal
        open={modalOpen}
        saving={saving}
        onCancel={() => setModalOpen(false)}
        onSubmit={handleCreate}
      />
    </>
  );
}

function renderImageHash(imageHash: string) {
  if (!imageHash) return <>Pending inspect</>;
  return <Tooltip content={imageHash}>{imageHash.slice(0, 12)}</Tooltip>;
}
