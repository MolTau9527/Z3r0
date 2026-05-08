import { useCallback, useEffect, useRef, useState } from "react";
import { showApiError } from "../api/feedback";

type QueryParams = {
  page: number;
  size: number;
  keyword: string;
};

type QueryResponse<Item> = {
  data?: {
    items: Item[];
  } | null;
};

type UsePagedResourceListOptions<Item> = {
  pageSize: number;
  query: (params: QueryParams) => Promise<QueryResponse<Item>>;
};

export function usePagedResourceList<Item>({ pageSize, query }: UsePagedResourceListOptions<Item>) {
  const [items, setItems] = useState<Item[]>([]);
  const [page, setPage] = useState(1);
  const [keyword, setKeyword] = useState("");
  const [activeKeyword, setActiveKeyword] = useState("");
  const [loading, setLoading] = useState(false);
  const requestIdRef = useRef(0);

  const loadItems = useCallback(async () => {
    const requestId = requestIdRef.current + 1;
    requestIdRef.current = requestId;
    setLoading(true);
    try {
      const response = await query({ page, size: pageSize, keyword: activeKeyword });
      if (requestIdRef.current !== requestId) return;
      const nextItems = response.data?.items || [];
      if (nextItems.length === 0 && page > 1) {
        setPage((current) => Math.max(1, current - 1));
        return;
      }
      setItems(nextItems);
    } catch (error) {
      if (requestIdRef.current === requestId) {
        showApiError(error);
      }
    } finally {
      if (requestIdRef.current === requestId) {
        setLoading(false);
      }
    }
  }, [activeKeyword, page, pageSize, query]);

  useEffect(() => {
    void loadItems();
  }, [loadItems]);

  const search = useCallback(() => {
    setPage(1);
    setActiveKeyword(keyword.trim());
  }, [keyword]);

  const previous = useCallback(() => {
    setPage((current) => Math.max(1, current - 1));
  }, []);

  const next = useCallback(() => {
    setPage((current) => current + 1);
  }, []);

  return {
    items,
    page,
    keyword,
    loading,
    loadItems,
    setKeyword,
    search,
    previous,
    next,
    canGoBack: page > 1,
    canGoNext: items.length === pageSize,
  };
}
