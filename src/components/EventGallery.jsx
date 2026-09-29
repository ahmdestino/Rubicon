import React, { useState, useEffect } from 'react';
import {
  Search,
  Eye,
  Download,
  Maximize2,
  Loader2,
} from 'lucide-react';
import { api } from '../api';
import { Pagination } from './ui/Pagination';

export const EventGallery = ({
  event,
  photos: initialPhotos = [],
  onOpenLightbox,
  onDownloadSingle,
}) => {
  const [selectedSession, setSelectedSession] = useState('All Sessions');
  const [searchQuery, setSearchQuery] = useState('');
  const [debouncedQuery, setDebouncedQuery] = useState('');
  const [sortBy, setSortBy] = useState('popular');
  const [currentPage, setCurrentPage] = useState(1);
  const [pageSize, setPageSize] = useState(24);

  const [galleryPhotos, setGalleryPhotos] = useState(initialPhotos);
  const [totalPhotosCount, setTotalPhotosCount] = useState(event?.totalPhotos || initialPhotos.length);
  const [totalPages, setTotalPages] = useState(1);
  const [isLoading, setIsLoading] = useState(false);

  // Debounce search query input (300ms) to avoid server spam on every keystroke
  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedQuery(searchQuery);
    }, 300);
    return () => clearTimeout(timer);
  }, [searchQuery]);

  // Reset to page 1 whenever filters or search query change
  useEffect(() => {
    setCurrentPage(1);
  }, [selectedSession, debouncedQuery, sortBy]);

  // Fetch page data from backend dynamically
  useEffect(() => {
    if (!event?.id) return;

    let isMounted = true;
    setIsLoading(true);

    const params = {
      page: currentPage,
      pageSize,
      sort: sortBy,
    };
    if (selectedSession && selectedSession !== 'All Sessions') {
      params.session = selectedSession;
    }
    if (debouncedQuery.trim()) {
      params.q = debouncedQuery.trim();
    }

    api.listPhotos(event.id, params)
      .then((res) => {
        if (!isMounted) return;
        const items = res.items || res;
        setGalleryPhotos(items);
        setTotalPhotosCount(res.total ?? items.length);
        setTotalPages(res.totalPages ?? (Math.ceil((res.total ?? items.length) / pageSize) || 1));
      })
      .catch((err) => {
        console.error('Failed to load gallery photos:', err);
      })
      .finally(() => {
        if (isMounted) setIsLoading(false);
      });

    return () => {
      isMounted = false;
    };
  }, [event?.id, currentPage, pageSize, selectedSession, debouncedQuery, sortBy]);

  const handlePageChange = (newPage) => {
    setCurrentPage(newPage);
    window.scrollTo({ top: 300, behavior: 'smooth' });
  };

  return (
    <div className="space-y-6">
      {/* Search and Filters Bar */}
      <div className="bg-white border border-slate-200 rounded-2xl p-4 sm:p-5 space-y-4 shadow-sm">
        <div className="flex flex-col md:flex-row gap-3 items-stretch md:items-center justify-between">
          {/* Search Input */}
          <div className="relative flex-1">
            <Search className="w-4 h-4 text-slate-400 absolute left-3.5 top-3" />
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search by participant name, session, or filename..."
              className="w-full bg-slate-50 border border-slate-200 rounded-xl pl-10 pr-4 py-2.5 text-xs text-slate-900 placeholder-slate-400 focus:outline-none focus:border-indigo-600 transition-colors"
            />
          </div>

          {/* Sort Selector */}
          <div className="flex items-center space-x-2">
            <span className="text-xs text-slate-500 shrink-0 font-medium">Sort by:</span>
            <select
              value={sortBy}
              onChange={(e) => setSortBy(e.target.value)}
              className="bg-white border border-slate-200 rounded-xl px-3 py-2 text-xs text-slate-900 focus:outline-none focus:border-indigo-600 cursor-pointer shadow-xs font-medium"
            >
              <option value="taken">Date Taken (newest)</option>
              <option value="newest">Recently Uploaded</option>
              <option value="popular">Most Viewed</option>
              <option value="downloads">Most Downloaded</option>
            </select>
          </div>
        </div>

        {/* Filter Pills */}
        <div className="flex flex-wrap items-center gap-2 pt-3 border-t border-slate-100 text-xs">
          <div className="flex items-center space-x-1.5 overflow-x-auto pb-1 max-w-full">
            <span className="text-slate-400 font-bold text-[11px] uppercase tracking-wider shrink-0">Sessions:</span>
            {(event?.sessions || ['All Sessions']).map((sess) => (
              <button
                key={sess}
                onClick={() => setSelectedSession(sess)}
                className={`px-3 py-1 rounded-lg text-xs transition-colors shrink-0 cursor-pointer ${
                  selectedSession === sess
                    ? 'bg-indigo-600 text-white font-bold shadow-sm'
                    : 'bg-slate-50 text-slate-600 hover:text-slate-900 border border-slate-200'
                }`}
              >
                {sess}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* Photo count header & Pagination top bar */}
      <div className="flex flex-col sm:flex-row items-start sm:items-center justify-between text-xs text-slate-500 px-1 gap-2">
        <span className="flex items-center space-x-2">
          <span>
            Page <strong className="text-slate-900 font-bold">{currentPage}</strong> of{' '}
            <strong className="text-slate-900 font-bold">{totalPages}</strong> &bull; Showing{' '}
            <strong className="text-slate-900 font-bold">{galleryPhotos.length}</strong> of{' '}
            <strong className="text-slate-900 font-bold">{totalPhotosCount.toLocaleString()}</strong> photos
          </span>
          {isLoading && <Loader2 className="w-3.5 h-3.5 text-indigo-600 animate-spin" />}
        </span>
        <span className="text-emerald-700 font-mono font-medium">Server-Paginated Engine</span>
      </div>

      {/* Empty state */}
      {!isLoading && galleryPhotos.length === 0 && (
        <div className="text-center py-16 bg-white rounded-2xl border border-slate-200 space-y-2 shadow-sm">
          <div className="text-sm font-semibold text-slate-700">No photos found</div>
          <div className="text-xs text-slate-500">
            {debouncedQuery || selectedSession !== 'All Sessions'
              ? 'Try adjusting your search query or session filters.'
              : 'Upload photos from the Admin Panel to populate this gallery.'}
          </div>
        </div>
      )}

      {/* Photos Masonry / Grid */}
      <div className={`grid grid-cols-1 sm:grid-cols-2 md:grid-cols-3 lg:grid-cols-4 gap-4 transition-opacity ${isLoading ? 'opacity-50 pointer-events-none' : 'opacity-100'}`}>
        {galleryPhotos.map((photo) => (
          <div
            key={photo.id}
            className="group relative bg-white rounded-2xl overflow-hidden border border-slate-200 hover:border-slate-300 shadow-sm transition-all"
          >
            <div
              className="relative aspect-4/3 overflow-hidden cursor-pointer bg-slate-900"
              onClick={() => onOpenLightbox(photo)}
            >
              <img
                src={photo.thumbnailUrl || photo.url}
                alt={photo.filename}
                loading="lazy"
                className="w-full h-full object-cover group-hover:scale-105 transition-transform duration-300"
              />

              {/* Hover overlay */}
              <div className="absolute inset-0 bg-gradient-to-t from-slate-950/85 via-transparent to-transparent opacity-0 group-hover:opacity-100 transition-opacity p-3 flex flex-col justify-between">
                <div className="flex justify-end">
                  <span className="p-1.5 rounded-lg bg-white/90 text-slate-900 backdrop-blur-sm shadow-sm">
                    <Maximize2 className="w-3.5 h-3.5" />
                  </span>
                </div>
                <div className="text-white text-xs">
                  <div className="font-bold truncate">{photo.sessionTag}</div>
                  <div className="text-[11px] text-slate-200 font-mono">
                    By {photo.photographerName}
                  </div>
                </div>
              </div>

              {/* Faces count tag */}
              <div className="absolute top-2.5 left-2.5 bg-white/95 backdrop-blur-md px-2 py-0.5 rounded text-[10px] text-slate-700 font-bold border border-slate-200 shadow-sm flex items-center space-x-1.5">
                <span className="w-1.5 h-1.5 rounded-full bg-indigo-600" />
                <span>{(photo.faces || []).length} Faces Tagged</span>
              </div>
            </div>

            {/* Footer */}
            <div className="p-3 bg-white border-t border-slate-100 flex items-center justify-between text-xs">
              <span className="text-[11px] text-slate-600 font-medium truncate max-w-[150px]" title={photo.filename}>
                {(photo.filename || '').replace(/^IMG_|^DSC_|^NIK_|^GFX_/, '')}
              </span>
              <div className="flex items-center space-x-1">
                <button
                  onClick={() => onOpenLightbox(photo)}
                  className="p-1 text-slate-400 hover:text-slate-900 rounded cursor-pointer"
                  title="View Details"
                >
                  <Eye className="w-3.5 h-3.5" />
                </button>
                <button
                  onClick={() => onDownloadSingle(photo)}
                  className="p-1 text-slate-400 hover:text-indigo-600 rounded cursor-pointer"
                  title="Download High-Res"
                >
                  <Download className="w-3.5 h-3.5" />
                </button>
              </div>
            </div>
          </div>
        ))}
      </div>

      {/* Pagination Controls */}
      <Pagination
        currentPage={currentPage}
        totalPages={totalPages}
        pageSize={pageSize}
        totalItems={totalPhotosCount}
        onPageChange={handlePageChange}
        onPageSizeChange={setPageSize}
        pageSizeOptions={[24, 48, 96]}
      />
    </div>
  );
};
