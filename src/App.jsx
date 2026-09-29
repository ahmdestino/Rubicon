import React, { useState, useEffect, useCallback } from 'react';
import { ArrowUpRight } from 'lucide-react';
import { api } from './api';
import { Navbar } from './components/Navbar';
import { ParticipantFinder } from './components/ParticipantFinder';
import { AdminPanel } from './components/AdminPanel';
import { EventGallery } from './components/EventGallery';
import { PhotoLightbox } from './components/PhotoLightbox';
import { ArchitectureModal } from './components/ArchitectureModal';

export default function App() {
  const [currentRole, setCurrentRole] = useState(() => {
    const path = window.location.pathname.toLowerCase().replace(/\/+$/, '');
    const params = new URLSearchParams(window.location.search);
    if (
      path === '/rubicon-ops' ||
      path === '/admin' ||
      path.endsWith('/rubicon-ops') ||
      path.endsWith('/admin') ||
      params.has('admin') ||
      params.has('ops') 
    ) {
      return 'admin';
    }
    return 'participant';
  });

  const [events, setEvents] = useState([]);
  const [currentEvent, setCurrentEvent] = useState(null);
  const [photographers, setPhotographers] = useState([]);
  const [participants, setParticipants] = useState([]);
  const [photos, setPhotos] = useState([]);
  const [storageConfig, setStorageConfig] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [lightboxPhoto, setLightboxPhoto] = useState(null);
  const [highlightedParticipantId, setHighlightedParticipantId] = useState(undefined);
  const [isArchitectureModalOpen, setIsArchitectureModalOpen] = useState(false);
  const [participantSubTab, setParticipantSubTab] = useState('find_my_photos');

  useEffect(() => {
    const path = window.location.pathname.toLowerCase().replace(/\/+$/, '');
    const params = new URLSearchParams(window.location.search);
    if (
      path === '/rubicon-ops' ||
      path === '/admin' ||
      path.endsWith('/rubicon-ops') ||
      path.endsWith('/admin') ||
      params.has('admin') ||
      params.has('ops')
    ) {
      setCurrentRole('admin');
    }
  }, []);

  // Load the event list once on mount.
  const loadEvents = useCallback(
    (selectId) =>
      api.listEvents().then((list) => {
        setEvents(list);
        setCurrentEvent((cur) => {
          if (selectId) return list.find((e) => e.id === selectId) || list[0] || null;
          return list.find((e) => e.id === cur?.id) || cur || list[0] || null;
        });
        if (!list.length) setLoading(false);
        return list;
      }),
    [],
  );

  useEffect(() => {
    loadEvents().catch((e) => { setError(e.message); setLoading(false); });
  }, [loadEvents]);

  // Load everything scoped to the selected event.
  const reloadEventData = useCallback((eventId) => {
    if (!eventId) return Promise.resolve();
    return Promise.all([
      api.listPhotographers(eventId),
      api.listParticipants(eventId),
      api.listPhotos(eventId, { pageSize: 48 }),
      api.storageStats(eventId).then((s) => s.provider).catch(() => null),
      api.getEvent(eventId),
    ]).then(([ph, pa, photoResp, storage, freshEvent]) => {
      setPhotographers(ph);
      setParticipants(pa);
      setPhotos(photoResp.items || []);
      setStorageConfig(storage);
      setCurrentEvent(freshEvent);
    });
  }, []);

  useEffect(() => {
    if (!currentEvent) return;
    setLoading(true);
    reloadEventData(currentEvent.id).finally(() => setLoading(false));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [currentEvent?.id, reloadEventData]);

  // After an upload, re-pull the real persisted photos + counters.
  const handleUploadPhotos = () => {
    if (currentEvent) reloadEventData(currentEvent.id);
  };

  const handleOpenLightbox = (photo, participantId) => {
    setLightboxPhoto(photo);
    setHighlightedParticipantId(participantId);
    if (photo?.id) api.bumpView(photo.id).catch(() => { });
  };

  // Real download: hits the backend (increments count) then streams the file as attachment.
  const handleDownloadSingle = (photo) => {
    if (!photo?.id) return;
    const url = api.photoDownloadUrl(photo.id);
    const link = document.createElement('a');
    link.href = url;
    link.download = photo.filename || `photo-${photo.id}.jpg`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
  };

  if (loading && !currentEvent) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-[#F8FAFC] text-slate-500">
        <div className="animate-pulse text-sm font-medium">Loading Rubicon…</div>
      </div>
    );
  }

  if (error || !currentEvent) {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center bg-[#F8FAFC] text-center px-6">
        <h1 className="text-xl font-extrabold text-slate-900 font-display">Rubicon backend not reachable</h1>
        <p className="mt-2 text-sm text-slate-500 max-w-md">
          {error || 'No events found.'} Start the Flask backend (see backend/README.md):
          <code className="block mt-3 bg-slate-900 text-slate-100 rounded-lg px-3 py-2 text-left">
            flask --app app init-db && flask --app app seed && flask --app app run -p 5000
          </code>
        </p>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-[#F8FAFC] text-slate-900 flex flex-col selection:bg-indigo-600 selection:text-white">
      <Navbar
        currentRole={currentRole}
        setCurrentRole={setCurrentRole}
        currentEvent={currentEvent}
        events={events}
        onSelectEvent={setCurrentEvent}
        storageConfig={storageConfig}
        onOpenArchitecture={() => setIsArchitectureModalOpen(true)}
        participantSubTab={participantSubTab}
        setParticipantSubTab={setParticipantSubTab}
      />

      <main className="flex-1">
        {currentRole === 'participant' && (
          <div>
            {participantSubTab === 'find_my_photos' ? (
              <ParticipantFinder
                event={currentEvent}
                participants={participants}
                photos={photos}
                onOpenLightbox={handleOpenLightbox}
              />
            ) : (
              <div className="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8 py-8">
                <EventGallery
                  event={currentEvent}
                  photos={photos}
                  photographers={photographers}
                  onOpenLightbox={handleOpenLightbox}
                  onDownloadSingle={handleDownloadSingle}
                />
              </div>
            )}
          </div>
        )}

        {currentRole !== 'participant' && (
          <AdminPanel
            event={currentEvent}
            events={events}
            photographers={photographers}
            participants={participants}
            photos={photos}
            storageConfig={storageConfig}
            onUploadPhotos={handleUploadPhotos}
            onDataChanged={() => currentEvent && reloadEventData(currentEvent.id)}
            onEventsChanged={loadEvents}
            onSelectEvent={setCurrentEvent}
            onOpenLightbox={handleOpenLightbox}
            onOpenArchitecture={() => setIsArchitectureModalOpen(true)}
            onSwitchToParticipantView={() => {
              setCurrentRole('participant');
              setParticipantSubTab('find_my_photos');
            }}
          />
        )}
      </main>

      <PhotoLightbox
        photo={lightboxPhoto}
        onClose={() => setLightboxPhoto(null)}
        onDownloadSingle={handleDownloadSingle}
        highlightedParticipantId={highlightedParticipantId}
      />

      <ArchitectureModal
        isOpen={isArchitectureModalOpen}
        onClose={() => setIsArchitectureModalOpen(false)}
      />

      <footer className="border-t border-slate-200/80 bg-white">
        <div className="max-w-6xl mx-auto px-4 sm:px-6 lg:px-8 py-7 flex flex-col sm:flex-row items-center justify-between gap-4">
          {/* Brand — mirrors the Navbar mark for a cohesive identity */}
          <div className="flex items-center gap-2.5">
            <div className="w-7 h-7 bg-indigo-600 rounded-lg flex items-center justify-center shadow-sm shrink-0">
              <div className="w-3 h-3 bg-white rotate-45" />
            </div>
            <div className="flex flex-col leading-tight">
              <span className="font-extrabold text-sm tracking-tight text-slate-900 font-display">
                RUBICON
              </span>
              <span className="text-[11px] text-slate-400">
                Let <span className="text-indigo-500 font-semibold">Ruby</span> find you in every moment
              </span>
            </div>
          </div>

          {/* Credit */}
          <div className="flex items-center gap-1.5 text-xs text-slate-500">
            <span>Built by</span>
            <a
              href="https://kingsleydestiny.vercel.app"
              target="_blank"
              rel="noopener noreferrer"
              className="group inline-flex items-center gap-0.5 font-semibold text-slate-700 hover:text-indigo-600 transition-colors"
            >
              Destiny Kingsley
              <ArrowUpRight className="w-3 h-3 text-slate-400 group-hover:text-indigo-600 transition-colors" />
            </a>
            <span className="text-slate-300">·</span>
            <span>2026</span>
          </div>
        </div>
      </footer>
    </div>
  );
}
