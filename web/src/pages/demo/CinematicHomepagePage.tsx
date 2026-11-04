import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  useCallback,
} from "react";
import { Link } from "react-router-dom";
import {
  ArrowLeft,
  ArrowRight,
  Check,
  Minus,
  Plus,
  RotateCcw,
  Search,
  X,
} from "lucide-react";
import gsap from "gsap";
import { useDestinations } from "@/hooks/useDestinations";
import { destinationsToMapPoints } from "@/utils/destinationTransform";
import { useCityPhotos } from "@/hooks/useCityPhotos";
import type { MapCityPoint } from "@/constants/chinaGeo";
import type {
  CinematicStep,
  CinematicRhythm,
  CinematicInterest,
} from "@/types/cinematic";
import type { RequestedCommuteMode } from "@/types/form";
import {
  formatDateLabel,
  getInclusiveDays,
} from "@/utils/cinematicDate";
import {
  loadCinematicDraft,
  saveCinematicDraft,
  switchDraftCity,
} from "@/utils/cinematicDraft";
import { CinematicMap } from "@/components/cinematic/CinematicMap";
import { CinematicCalendar } from "@/components/cinematic/CinematicCalendar";
import { CinematicCompanions } from "@/components/cinematic/CinematicCompanions";
import { CinematicPreferences } from "@/components/cinematic/CinematicPreferences";
import { CinematicCommute } from "@/components/cinematic/CinematicCommute";
import { CinematicReview } from "@/components/cinematic/CinematicReview";
import { CinematicPoiPicker } from "@/components/cinematic/CinematicPoiPicker";
import { useNavigate } from "react-router-dom";
import { submitTrip } from "@/services/api";
import { useTripStore } from "@/stores/tripStore";
import { useTripTaskStore } from "@/stores/tripTaskStore";
import { checkPoiSelection } from "@/api/poi";
import { showToast } from "@/stores/toastStore";
import { saveCinematicSubmission, clearCinematicSubmission } from "@/utils/cinematicSubmission";
import { validateDateRange } from "@/utils/cinematicDate";
import "@/components/cinematic/cinematic.css";

type SelectableCity = Omit<MapCityPoint, "x" | "y" | "lng" | "lat"> & Partial<Pick<MapCityPoint, "x" | "y" | "lng" | "lat">>;

const clamp = (val: number, min: number, max: number) =>
  Math.min(max, Math.max(min, val));

export default function CinematicHomepagePage() {
  const { data: destinationsData } = useDestinations();

  const cities = useMemo(() => {
    if (destinationsData?.destinations) {
      return destinationsToMapPoints(destinationsData.destinations);
    }
    return [];
  }, [destinationsData]);

  // Selection uses the complete directory. Missing map coordinates must not hide a city.
  const selectableCities = useMemo<SelectableCity[]>(() =>
    (destinationsData?.destinations || []).map(d => cities.find(c => c.name === d.name) || {
      name: d.name, enName: d.nameEn, iata: d.iataCode, region: d.region,
      tag: d.tagline, desc: d.description,
    }), [destinationsData, cities]);

  // Destination ID lookup
  const getDestinationForCity = useCallback(
    (cityName?: string, cityIata?: string) => {
      if (!destinationsData?.destinations) return null;
      return (
        destinationsData.destinations.find(
          (d) =>
            (cityName && d.name === cityName) ||
            (cityIata && d.iataCode === cityIata),
        ) || null
      );
    },
    [destinationsData],
  );

  const getCityId = useCallback(
    (city: SelectableCity | null): string => {
      if (!city) return "";
      const dest = getDestinationForCity(city.name, city.iata);
      return dest?.id || city.iata || city.name;
    },
    [getDestinationForCity],
  );

  // City selection state: preview on map vs confirmed in draft
  const [previewCity, setPreviewCity] = useState<SelectableCity | null>(null);
  const [confirmedCity, setConfirmedCity] = useState<SelectableCity | null>(null);

  // Form states
  const [step, setStep] = useState<CinematicStep>("dates");
  const [startDate, setStartDate] = useState("");
  const [endDate, setEndDate] = useState("");
  const [companion, setCompanion] = useState("");
  const [peopleCount, setPeopleCount] = useState(1);
  const [selectedRhythm, setSelectedRhythm] = useState<CinematicRhythm>("适中");
  const [selectedInterests, setSelectedInterests] = useState<string[]>([]);
  const [commuteMode, setCommuteMode] = useState<RequestedCommuteMode>("driving");
  const [accommodationName, setAccommodationName] = useState("");
  const [notes, setNotes] = useState("");
  const [fromCity, setFromCity] = useState("");
  const [avoid, setAvoid] = useState<string[]>([]);
  const [dailyStart, setDailyStart] = useState<string | undefined>();
  const [dailyEnd, setDailyEnd] = useState<string | undefined>();
  const [budget, setBudget] = useState<number | undefined>();

  // Map and camera references
  const rootRef = useRef<HTMLDivElement>(null);
  const svgRef = useRef<SVGSVGElement>(null);
  const cameraRef = useRef<SVGGElement>(null);
  const timelineRef = useRef<gsap.core.Timeline | null>(null);
  const targetImmersedRef = useRef(false);
  const enterButtonRef = useRef<HTMLButtonElement>(null);
  const questionTitleRef = useRef<HTMLHeadingElement>(null);

  // Interactive UI states
  const [immersed, setImmersed] = useState(false);
  const [targetImmersed, setTargetImmersed] = useState(false);
  const [moving, setMoving] = useState(false);
  const [view, setView] = useState({ x: 0, y: 0, zoom: 1 });
  const viewRef = useRef(view);
  const [searchOpen, setSearchOpen] = useState(false);
  const [searchQuery, setSearchQuery] = useState("");
  const [reducedMotion, setReducedMotion] = useState(
    () => typeof window !== "undefined" && window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  );

  // Gesture refs
  const pointersRef = useRef(new Map<number, { x: number; y: number }>());
  const gestureRef = useRef({ moved: false, x: 0, y: 0, gap: 0 });
  const [dragging, setDragging] = useState(false);

  // Photo library for previewed city
  const rawCityPhotos = useCityPhotos(previewCity?.name || "");
  const cityPhotos = rawCityPhotos.filter(p => !p.includes("city-placeholder.svg"));
  const [photoIndex, setPhotoIndex] = useState(0);
  const desiredPhoto = cityPhotos[photoIndex % cityPhotos.length] || "";
  const [mountedPhoto, setMountedPhoto] = useState({ src: "", city: "", previous: "" });
  const [photoError, setPhotoError] = useState(false);
  const [retryCount, setRetryCount] = useState(0);

  // Integration state
  const navigate = useNavigate();
  const setJob = useTripStore((s) => s.setJob);
  
  const [selectedPois, setSelectedPois] = useState<{ id: number; name: string }[]>([]);
  const [poiPickerOpen, setPoiPickerOpen] = useState(false);
  const [isSubmitting, setIsSubmitting] = useState(false);

  const photoReady =
    mountedPhoto.src === desiredPhoto &&
    mountedPhoto.city === (previewCity?.name || "") &&
    !photoError;

  // Deliberate initial draft load (runs once when cities/destinations are available)
  const initializedRef = useRef(false);

  useEffect(() => {
    if (initializedRef.current || selectableCities.length === 0) return;
    initializedRef.current = true;

    const savedDraft = loadCinematicDraft();
    if (savedDraft && (savedDraft.cityId || savedDraft.cityName)) {
      const matchedDest = destinationsData?.destinations?.find(
        (d) =>
          (savedDraft.cityId && d.id === savedDraft.cityId) ||
          (savedDraft.cityName && d.name === savedDraft.cityName),
      );
      const match = selectableCities.find(
        (c) =>
          (matchedDest && c.name === matchedDest.name) ||
          (savedDraft.cityName && c.name === savedDraft.cityName) ||
          (savedDraft.cityId && (c.iata === savedDraft.cityId || c.name === savedDraft.cityId)),
      );

      if (match) {
        setPreviewCity(match);
        setConfirmedCity(match);
        setStep(savedDraft.step || "dates");
        setStartDate(savedDraft.form.start_date || "");
        setEndDate(savedDraft.form.end_date || "");
        setPeopleCount(savedDraft.form.people_count || 1);
        if (savedDraft.form.people_count === 1) setCompanion("独自旅行");
        else if (savedDraft.form.people_count === 2) setCompanion("两个人");
        else setCompanion("和朋友");

        if (savedDraft.form.preferences) {
          const rhythm = savedDraft.form.preferences.find(
            (p): p is CinematicRhythm => p === "轻松" || p === "适中" || p === "紧凑",
          );
          if (rhythm) setSelectedRhythm(rhythm);
          setSelectedInterests(savedDraft.form.preferences.filter((p) => p !== rhythm));
        }

        setCommuteMode(savedDraft.form.commute_mode || "driving");
        setAccommodationName(savedDraft.form.accommodation?.name || "");
        setNotes(savedDraft.form.notes || "");
        setFromCity(savedDraft.form.from_city || "");
        setAvoid(savedDraft.form.avoid || []);
        setDailyStart(savedDraft.form.daily_start);
        setDailyEnd(savedDraft.form.daily_end);
        setBudget(savedDraft.form.budget);
        setSelectedPois(savedDraft.selectedPois || []);
        return;
      }
    }

    // Default to Chongqing or first city
    const chongqing = selectableCities.find((c) => c.name === "重庆");
    const defaultCity = chongqing || selectableCities[0];
    setPreviewCity(defaultCity);
    setConfirmedCity(defaultCity);
  }, [selectableCities, destinationsData]);

  // Persist draft updates (scoped strictly to confirmed city)
  useEffect(() => {
    if (!confirmedCity) return;

    const cityId = getCityId(confirmedCity);
    const preferences = [...selectedInterests];
    if (!preferences.includes(selectedRhythm)) {
      preferences.push(selectedRhythm);
    }

    const days = startDate && endDate ? getInclusiveDays(startDate, endDate) : 1;

    const draft = {
      version: 1 as const,
      step,
      cityId,
      cityName: confirmedCity.name,
      selectedPois,
      returnLayer: null,
      ownerUserId: null,
      intent: null,
      form: {
        to_city: confirmedCity.name,
        from_city: fromCity || undefined,
        start_date: startDate,
        end_date: endDate,
        days,
        people_count: peopleCount,
        preferences,
        avoid,
        notes,
        commute_mode: commuteMode,
        accommodation: accommodationName ? { name: accommodationName } : undefined,
        daily_start: dailyStart,
        daily_end: dailyEnd,
        budget,
      },
      updatedAt: Date.now(),
    };

    saveCinematicDraft(draft);
  }, [
    confirmedCity,
    getCityId,
    step,
    startDate,
    endDate,
    peopleCount,
    selectedRhythm,
    selectedInterests,
    commuteMode,
    accommodationName,
    notes,
    fromCity,
    avoid,
    dailyStart,
    dailyEnd,
    budget,
    selectedPois,
  ]);

  // Monitor prefers-reduced-motion
  useEffect(() => {
    if (typeof window === "undefined") return;
    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    const handler = () => setReducedMotion(media.matches);
    media.addEventListener("change", handler);
    return () => media.removeEventListener("change", handler);
  }, []);

  // Preload real photos
  useEffect(() => {
    if (!previewCity) return;
    
    if (!desiredPhoto) {
      setMountedPhoto({ src: "", city: previewCity.name, previous: "" });
      setPhotoError(false);
      return;
    }

    const currentCityName = previewCity.name;
    let active = true;
    const img = new Image();
    setPhotoError(false);

    const timer = window.setTimeout(() => {
      if (active) setPhotoError(true);
    }, 12000);

    img.onload = async () => {
      try {
        await img.decode();
      } catch {
        // Image may already be decoded
      }
      if (active) {
        window.clearTimeout(timer);
        setMountedPhoto((old) => ({
          src: desiredPhoto,
          city: currentCityName,
          previous: old.src === desiredPhoto ? "" : old.src,
        }));
        setPhotoError(false);
      }
    };

    img.onerror = () => {
      if (active) {
        window.clearTimeout(timer);
        setPhotoError(true);
      }
    };

    img.src = desiredPhoto;

    return () => {
      active = false;
      window.clearTimeout(timer);
      img.onload = null;
      img.onerror = null;
    };
  }, [desiredPhoto, previewCity, retryCount]);

  // GSAP transition timeline setup
  useLayoutEffect(() => {
    if (!previewCity) return;

    const ctx = gsap.context(() => {
      const flight = gsap.timeline({
        paused: true,
        onComplete: () => {
          setMoving(false);
          questionTitleRef.current?.focus({ preventScroll: true });
        },
        onReverseComplete: () => {
          setMoving(false);
          enterButtonRef.current?.focus({ preventScroll: true });
        },
      });

      const duration = reducedMotion ? 0.16 : 1.45;

      flight
        .to(
          ".cmp-map-chrome",
          { opacity: 0, y: reducedMotion ? 0 : -12, duration: duration * 0.25 },
          0,
        )
        .to(
          cameraRef.current,
          {
            scale: reducedMotion ? 1 : 3.2,
            svgOrigin: `${previewCity.x ?? 450} ${previewCity.y ?? 350}`,
            duration: duration * 0.86,
            ease: "power3.inOut",
          },
          0,
        )
        .to(
          ".cmp-atlas",
          { opacity: 0, duration: duration * 0.5, ease: "power2.in" },
          duration * 0.18,
        )
        .to(
          ".cmp-map-shade",
          { opacity: 0, duration: duration * 0.85, ease: "power2.inOut" },
          0,
        )
        .to(
          ".cmp-photo-depth",
          { scale: reducedMotion ? 1 : 1.045, duration, ease: "power2.inOut" },
          0,
        )
        .fromTo(
          ".cmp-conversation",
          { autoAlpha: 0, y: reducedMotion ? 0 : 26 },
          {
            autoAlpha: 1,
            y: 0,
            duration: duration * 0.4,
            ease: "power2.out",
          },
          duration * 0.66,
        );

      timelineRef.current = flight;
      if (targetImmersedRef.current) flight.progress(1);
    }, rootRef);

    return () => {
      timelineRef.current = null;
      ctx.revert();
    };
  }, [previewCity, reducedMotion]);

  // Focus header when stepping
  useEffect(() => {
    if (immersed && !moving) {
      questionTitleRef.current?.focus({ preventScroll: true });
    }
  }, [step, immersed, moving]);

  const updateView = useCallback((next: { x: number; y: number; zoom: number }) => {
    viewRef.current = next;
    setView(next);
  }, []);

  const zoomTo = useCallback((amount: number, anchor = { x: 450, y: 350 }, smooth = false) => {
    const old = viewRef.current;
    const zoom = clamp(amount, 0.85, 3.5);
    const ratio = zoom / old.zoom;
    const nextX = clamp(anchor.x - (anchor.x - old.x) * ratio, -1600, 500);
    const nextY = clamp(anchor.y - (anchor.y - old.y) * ratio, -1300, 450);

    if (smooth) {
      gsap.to(old, {
        zoom,
        x: nextX,
        y: nextY,
        duration: 0.35,
        ease: "power2.out",
        onUpdate: () => updateView({ ...old }),
      });
    } else {
      updateView({ zoom, x: nextX, y: nextY });
    }
  }, [updateView]);

  // Wheel zoom on map
  useEffect(() => {
    const el = svgRef.current;
    if (!el || immersed) return;

    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const mult = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 300 : 1;
      const matrix = typeof el.getScreenCTM === 'function' ? el.getScreenCTM() : null;
      const pt = matrix
        ? new DOMPoint(e.clientX, e.clientY).matrixTransform(matrix.inverse())
        : new DOMPoint(450, 350);

      zoomTo(
        viewRef.current.zoom * Math.exp(-e.deltaY * mult * 0.0015),
        pt,
      );
    };

    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [immersed, zoomTo]);

  // City selection preview (does not switch draft city or clear draft)
  const handleSelectCity = useCallback((city: SelectableCity | null) => {
    if (immersed || moving) return;
    setPreviewCity(city);
    setPhotoIndex(0);
    setSearchOpen(false);
    setSearchQuery("");
    if (city && city.x !== undefined && city.y !== undefined) {
      const targetZoom = Math.max(viewRef.current.zoom, 1.6);
      const nextX = clamp(450 - city.x * targetZoom, -1600, 500);
      const nextY = clamp(350 - city.y * targetZoom, -1300, 450);
      
      const old = viewRef.current;
      gsap.to(old, {
        zoom: targetZoom,
        x: nextX,
        y: nextY,
        duration: 0.4,
        ease: "power2.out",
        onUpdate: () => updateView({ ...old }),
      });
    }
  }, [immersed, moving, updateView]);

  const handleMapClick = useCallback((clientX: number, clientY: number) => {
    const el = svgRef.current;
    const matrix = typeof el?.getScreenCTM === 'function' ? el.getScreenCTM() : null;

    if (!matrix) return;

    // Calculate screen distances to all cities
    const screenCities = cities.map(city => {
      const svgX = city.x * viewRef.current.zoom + viewRef.current.x;
      const svgY = city.y * viewRef.current.zoom + viewRef.current.y;
      const screenPt = new DOMPoint(svgX, svgY).matrixTransform(matrix);
      const dist = Math.hypot(screenPt.x - clientX, screenPt.y - clientY);
      return { city, dist };
    });

    const nearby = screenCities.filter(c => c.dist <= 48).sort((a, b) => a.dist - b.dist);

    if (nearby.length === 0) {
      // 4. No nearby cities, zoom in around click
      const pt = new DOMPoint(clientX, clientY).matrixTransform(matrix.inverse());
      zoomTo(viewRef.current.zoom * 1.5, pt, true);
    } else if (nearby.length === 1) {
      // 2. Clear winner
      handleSelectCity(nearby[0].city);
    } else {
      // 3. Multiple nearby, check if too close
      const closest = nearby[0];
      const second = nearby[1];
      if (second.dist - closest.dist < 15) {
        // Too close to clearly distinguish, zoom in instead
        const pt = new DOMPoint(clientX, clientY).matrixTransform(matrix.inverse());
        zoomTo(viewRef.current.zoom * 1.5, pt, true);
      } else {
        handleSelectCity(closest.city);
      }
    }
  }, [cities, handleSelectCity, zoomTo]);

  const handleReset = () => {
    const old = viewRef.current;
    gsap.to(old, {
      zoom: 1,
      x: 0,
      y: 0,
      duration: 0.4,
      ease: "power2.out",
      onUpdate: () => updateView({ ...old }),
    });
  };

  // Enter city flow (confirms city selection and enters wizard)
  const enterFlow = () => {
    if (submissionLock.current || moving || !previewCity) return;

    // Confirm city switch if preview city differs from confirmed draft city
    if (confirmedCity && confirmedCity.name !== previewCity.name) {
      submissionIdRef.current++;
      const currentDraft = loadCinematicDraft();
      const newCityId = getCityId(previewCity);
      if (currentDraft) {
        const updated = switchDraftCity(
          currentDraft,
          newCityId,
          previewCity.name,
        );
        saveCinematicDraft(updated);
      }
      // Clear city-scoped fields only after confirmation
      // Clear React state in the same update as the city. Otherwise persistence
      // writes the previous city's POIs back over switchDraftCity's clean draft.
      setSelectedPois([]);
      setAccommodationName("");
      setPoiPickerOpen(false);
      clearCinematicSubmission();
    }

    setConfirmedCity(previewCity);

    pointersRef.current.clear();
    setDragging(false);
    setSearchOpen(false);
    targetImmersedRef.current = true;
    setTargetImmersed(true);
    setImmersed(true);
    setMoving(true);
    timelineRef.current?.play();
  };

  // Leave city flow (reversible from current progress)
  const leaveFlow = () => {
    if (submissionLock.current) return;
    targetImmersedRef.current = false;
    setTargetImmersed(false);
    setImmersed(false);
    setMoving(true);
    timelineRef.current?.reverse();
  };

  const handleNextStep = () => {
    if (step === "dates") setStep("companions");
    else if (step === "companions") setStep("preferences");
    else if (step === "preferences") setStep("commute");
    else if (step === "commute") setStep("review");
  };

  const handlePrevStep = () => {
    if (step === "companions") setStep("dates");
    else if (step === "preferences") setStep("companions");
    else if (step === "commute") setStep("preferences");
    else if (step === "review") setStep("commute");
  };

  const handleToggleInterest = (interest: CinematicInterest) => {
    setSelectedInterests((prev) =>
      prev.includes(interest) ? prev.filter((i) => i !== interest) : [...prev, interest],
    );
  };

  const handleClearExtraSettings = () => {
    setAvoid([]);
    setDailyStart(undefined);
    setDailyEnd(undefined);
    setBudget(undefined);
  };

  const handleOpenPoiPicker = () => {
    setPoiPickerOpen(true);
  };

  const submissionIdRef = useRef<number>(0);
  const submissionLock = useRef(false);

  const handleSubmitTrip = useCallback(async () => {
    if (submissionLock.current || !confirmedCity) return;
    const currentSubmissionId = ++submissionIdRef.current;

    if (!startDate || !endDate) {
      showToast("请选择行程日期", "error");
      return;
    }

    const { valid, error, days } = validateDateRange(startDate, endDate);
    if (!valid) {
      showToast(error || "行程日期无效", "error");
      return;
    }
    if (days < 1) {
      showToast("行程日期范围无效", "error");
      return;
    }

    submissionLock.current = true;
    setIsSubmitting(true);
    const stillCurrent = () => submissionIdRef.current === currentSubmissionId;

    try {
      let mustIncludeList: { place_id: number; name: string }[] | undefined = undefined;

      // Validate POIs before submit
      if (selectedPois.length > 0) {
        const res = await checkPoiSelection(confirmedCity.name, selectedPois.map(p => p.id));
        if (!stillCurrent()) return;
        const validItems = res.items.filter(i => i.status === "available");
        
        let changed = false;
        if (validItems.length !== selectedPois.length) changed = true;
        for (const sp of selectedPois) {
          const si = validItems.find(i => i.place_id === sp.id);
          if (si && si.place.name !== sp.name) changed = true;
        }

        if (changed) {
          showToast("地点状态或名称有更新，请打开选择器检查确认", "error");
          setIsSubmitting(false);
          return;
        }
        mustIncludeList = validItems.map(i => ({ place_id: i.place_id, name: i.place.name }));
      }

      const formDraft: import("@/types/form").TripFormData = {
        to_city: confirmedCity.name,
        start_date: startDate,
        end_date: endDate,
        days,
        people_count: peopleCount,
        preferences: [...selectedInterests, selectedRhythm],
        commute_mode: commuteMode,
        must_include: mustIncludeList,
        accommodation: accommodationName ? { name: accommodationName } : undefined,
        notes,
        from_city: fromCity || undefined,
        avoid,
        daily_start: dailyStart,
        daily_end: dailyEnd,
        budget,
      };

      if (!stillCurrent()) return;
      const submission = saveCinematicSubmission(formDraft, "guest");
      const submitRes = await submitTrip(formDraft, submission.request_id);
      
      if (!stillCurrent()) return;
      if (submitRes.job_id) {
        useTripStore.getState().setFormData(formDraft);
        useTripStore.getState().clearResult();
        useTripTaskStore.getState().addOrUpdateTask({
          jobId: submitRes.job_id,
          requestId: submission.request_id,
          destination: confirmedCity.name,
          startedAt: Date.now(),
          status: "pending",
          notificationState: "none",
        });
        setJob(submitRes.job_id, "QUEUED", null);
        clearCinematicSubmission();
        navigate(`/planning/${submitRes.job_id}`);
      }
    } catch (err: unknown) {
      if (!stillCurrent()) return;
      const msg = err instanceof Error ? err.message : "提交失败，请重试";
      showToast(msg, "error");
    } finally {
      if (submissionIdRef.current === currentSubmissionId) submissionLock.current = false;
      setIsSubmitting(false);
    }
  }, [
    confirmedCity, startDate, endDate, navigate, 
    selectedPois, peopleCount, selectedInterests, selectedRhythm, 
    commuteMode, accommodationName, notes, fromCity, avoid, 
    dailyStart, dailyEnd, budget, setJob
  ]);

  const dateSummary =
    startDate && endDate
      ? `${formatDateLabel(startDate)} — ${formatDateLabel(endDate)}`
      : "日期";

  const allPreferences = useMemo(() => {
    const list = [...selectedInterests];
    if (!list.includes(selectedRhythm)) list.unshift(selectedRhythm);
    return list;
  }, [selectedInterests, selectedRhythm]);

  const daysCount =
    startDate && endDate ? getInclusiveDays(startDate, endDate) : 1;

  const activeCity = confirmedCity || previewCity;

  return (
    <div
      ref={rootRef}
      className="cmp"
      data-view={immersed ? "destination" : "map"}
      data-moving={moving}
      data-step={step}
    >
      {/* Background Photos */}
      <div className="cmp-photo-depth" aria-hidden="true">
        {mountedPhoto.previous && (
          <img
            className="cmp-photo cmp-photo-previous"
            src={mountedPhoto.previous}
            alt=""
          />
        )}
        {mountedPhoto.src && !photoError && (
          <img
            key={mountedPhoto.src}
            className="cmp-photo"
            src={mountedPhoto.src}
            alt=""
            onAnimationEnd={(e) => {
              if (e.animationName === "cmp-photo-appear") {
                setMountedPhoto((old) => ({ ...old, previous: "" }));
              }
            }}
          />
        )}
        {(!mountedPhoto.src || photoError) && (
          <div className="cmp-photo-fallback">
            <span style={{ fontSize: "14px", opacity: 0.6 }}>
              {!desiredPhoto ? "这座城市暂未配置风景图，仍可继续规划" : photoError ? "背景风景加载受阻，可重试或直接启程" : "正在连线城市图库…"}
            </span>
          </div>
        )}
      </div>

      <div className="cmp-base-shade" />
      <div className="cmp-map-shade" />
      <div className="cmp-reading-shade" />

      {/* Header */}
      <header className="cmp-header">
        <Link to="/" className="cmp-brand" aria-label="云途，返回主页">
          <span>云途</span>
          <i /> <small>YUNTU</small>
        </Link>
        <div className="cmp-header-right">
          <Link to="/explore">手机规划</Link>
        </div>
      </header>

      {/* Intro on Map */}
      <div className="cmp-map-chrome cmp-intro" aria-hidden={immersed}>
        <span className="cmp-eyebrow">EXPLORE CHINA WITH CINEMATIC ATLAS</span>
        <h1>下一段旅程，从哪里开始？</h1>
        <p>轻抚山河，选一座心动的城市。</p>
      </div>

      {/* Interactive Map */}
      <CinematicMap
        cities={cities}
        selectedCity={cities.find(c => c.name === previewCity?.name) || null}
        onSelectCity={handleSelectCity}
        onMapClick={handleMapClick}
        cameraRef={cameraRef}
        svgRef={svgRef}
        view={view}
        onUpdateView={updateView}
        dragging={dragging}
        setDragging={setDragging}
        pointersRef={pointersRef}
        gestureRef={gestureRef}
        immersed={immersed}
        moving={moving}
      />

      {/* Selected Destination Card (Map Mode) */}
      {previewCity && (
        <section
          className="cmp-map-chrome cmp-destination"
          aria-hidden={immersed}
        >
          <span className="cmp-eyebrow">
            {(previewCity.enName || "").toUpperCase()}
            {previewCity.iata ? ` / ${previewCity.iata}` : ""}
          </span>
          <h2>
            {previewCity.name}
            <span>等你出发</span>
          </h2>
          <p>
            {previewCity.desc || previewCity.tag || "山水风华，步步皆景。"}
          </p>
          <div className="cmp-destination-actions">
            <button
              ref={enterButtonRef}
              type="button"
              className="cmp-enter"
              onClick={enterFlow}
              disabled={immersed || moving}
              tabIndex={immersed ? -1 : 0}
            >
              {photoError
                ? "去这里（无背景图）"
                : photoReady
                  ? "去这里"
                  : "载入风景中…"}
              <ArrowRight size={17} />
            </button>
            {photoError && (
              <button
                type="button"
                className="cmp-retry"
                onClick={() => setRetryCount((c) => c + 1)}
                tabIndex={immersed ? -1 : 0}
              >
                重试背景
              </button>
            )}
          </div>
        </section>
      )}

      {/* Map Tools */}
      <div className="cmp-map-chrome cmp-map-tools" aria-hidden={immersed}>
        <button
          type="button"
          aria-label="搜索城市"
          disabled={immersed || moving}
          onClick={() => setSearchOpen((v) => !v)}
        >
          <Search size={18} />
        </button>
        <i />
        <button
          type="button"
          aria-label="缩小地图"
          disabled={immersed || moving}
          onClick={() => zoomTo(view.zoom / 1.25, { x: 450, y: 350 }, true)}
        >
          <Minus size={18} />
        </button>
        <button
          type="button"
          aria-label="放大地图"
          disabled={immersed || moving}
          onClick={() => zoomTo(view.zoom * 1.25, { x: 450, y: 350 }, true)}
        >
          <Plus size={18} />
        </button>
        <button
          type="button"
          aria-label="重置地图"
          disabled={immersed || moving}
          onClick={handleReset}
        >
          <RotateCcw size={16} />
        </button>
      </div>

      <p className="cmp-map-chrome cmp-map-hint">
        拖动探索 <span>·</span> 点击光点选择目的地
      </p>

      {/* Search City Modal */}
      {searchOpen && !immersed && (
        <section className="cmp-search" aria-label="搜索目的地城市">
          <div className="cmp-search-input-row">
            <Search size={16} style={{ opacity: 0.6 }} />
            <input
              autoFocus
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="搜索目的地城市…"
              aria-label="目的地城市搜索"
              onKeyDown={(e) => {
                if (e.key === "Escape") setSearchOpen(false);
              }}
            />
            <button
              type="button"
              aria-label="关闭搜索"
              onClick={() => setSearchOpen(false)}
            >
              <X size={16} />
            </button>
          </div>
          <div className="cmp-search-results">
            {selectableCities
              .filter((c) =>
                `${c.name} ${c.enName || ""}`
                  .toLowerCase()
                  .includes(searchQuery.toLowerCase()),
              )
              .map((c) => (
                <button
                  key={c.name}
                  type="button"
                  className={`cmp-search-item ${c.name === previewCity?.name ? "is-active" : ""}`}
                  onClick={() => handleSelectCity(c)}
                >
                  <span>{c.name}</span>
                  {c.name === previewCity?.name && <Check size={14} />}
                </button>
              ))}
          </div>
          {!selectableCities.some((c) =>
            `${c.name} ${c.enName || ""}`
              .toLowerCase()
              .includes(searchQuery.toLowerCase()),
          ) && <div className="cmp-search-empty">暂无匹配城市</div>}
        </section>
      )}

      {/* Reversible Back to Map Button */}
      {(immersed || moving) && (
        <button
          type="button"
          className="cmp-back"
          onClick={leaveFlow}
          disabled={isSubmitting || !targetImmersed}
        >
          <ArrowLeft size={16} /> 返回地图
        </button>
      )}

      {/* Conversation Wizard Area */}
      <main
        className="cmp-conversation"
        aria-hidden={!immersed || moving}
        style={{ pointerEvents: immersed && !moving ? "auto" : "none" }}
      >
        {activeCity && (
          <div className="cmp-question" key={step}>
            <span className="cmp-eyebrow">
              {(activeCity.enName || "").toUpperCase()} · YOUR NEXT TRIP
            </span>
            <h2 ref={questionTitleRef} tabIndex={-1}>
              {step === "dates" && `什么时候，去${activeCity.name}走走？`}
              {step === "companions" && "这次旅途，和谁同行？"}
              {step === "preferences" && "想留下怎样的旅行记忆？"}
              {step === "commute" && "在城里漫步，你偏爱怎样的出行？"}
              {step === "review" && "这段旅程，开始有了模样。"}
            </h2>
            <p className="cmp-question-note">
              {step === "dates" && "圈出几天，把时间留给远方（最多 7 天）。"}
              {step === "companions" && "一个人的自由，或是一群人的热闹。"}
              {step === "preferences" && "挑选舒服的节奏与心动的体验。"}
              {step === "commute" && "选择市内出行偏好，规划更顺路。"}
              {step === "review" && "行程参数已准备就绪，随时启程。"}
            </p>

            {immersed && !moving && (
              <>
                {step === "dates" && (
                  <CinematicCalendar
                    start={startDate}
                    end={endDate}
                    onChange={(s, e) => {
                      setStartDate(s);
                      setEndDate(e);
                    }}
                  />
                )}

                {step === "companions" && (
                  <CinematicCompanions
                    companion={companion}
                    peopleCount={peopleCount}
                    onSelectCompanion={(vibe, count) => {
                      setCompanion(vibe);
                      setPeopleCount(count);
                    }}
                    onChangePeopleCount={setPeopleCount}
                  />
                )}

                {step === "preferences" && (
                  <CinematicPreferences
                    selectedInterests={selectedInterests}
                    selectedRhythm={selectedRhythm}
                    onToggleInterest={handleToggleInterest}
                    onSelectRhythm={setSelectedRhythm}
                  />
                )}

                {step === "commute" && (
                  <CinematicCommute
                    commuteMode={commuteMode}
                    onSelectCommuteMode={setCommuteMode}
                  />
                )}

                {step === "review" && (
                  <CinematicReview
                    cityName={activeCity.name}
                    startDate={startDate}
                    endDate={endDate}
                    days={daysCount}
                    companion={companion}
                    peopleCount={peopleCount}
                    commuteMode={commuteMode}
                    preferences={allPreferences}
                    accommodationName={accommodationName}
                    notes={notes}
                    fromCity={fromCity}
                    avoid={avoid}
                    dailyStart={dailyStart}
                    dailyEnd={dailyEnd}
                    budget={budget}
                    selectedPois={selectedPois}
                    onSaveAccommodation={setAccommodationName}
                    onSaveNotesAndFromCity={(n, f) => {
                      setNotes(n);
                      setFromCity(f);
                    }}
                    onClearExtraSettings={handleClearExtraSettings}
                    onBackToMap={leaveFlow}
                    onOpenPoiPicker={handleOpenPoiPicker} isSubmitting={isSubmitting}
                    onSubmit={handleSubmitTrip}
                  />
                )}

                <div className="cmp-question-actions">
                  {step !== "dates" && (
                    <button
                      type="button"
                      className="cmp-text-button"
                      disabled={isSubmitting} onClick={handlePrevStep}
                    >
                      <ArrowLeft size={15} /> 上一步
                    </button>
                  )}

                  {step !== "review" && (
                    <button
                      type="button"
                      className="cmp-continue"
                      disabled={
                        step === "dates"
                          ? !startDate || !endDate
                          : step === "companions"
                            ? !companion
                            : false
                      }
                      onClick={handleNextStep}
                    >
                      继续
                      <ArrowRight size={17} />
                    </button>
                  )}
                </div>
              </>
            )}
          </div>
        )}
      </main>

      {/* Footer */}
      <footer className="cmp-footer">
        <div className="cmp-photo-caption">
          <span>
            {previewCity?.name || "目的地"} <i /> 城市印象
          </span>
          {cityPhotos.length > 1 && (
            <div
              className="cmp-photo-dots"
              aria-label={`${previewCity?.name || ""} 背景照片切换`}
            >
              {cityPhotos.map((_, idx) => (
                <button
                  key={idx}
                  type="button"
                  aria-label={`切换到第 ${idx + 1} 张背景照片`}
                  aria-pressed={idx === photoIndex}
                  disabled={immersed || moving}
                  onClick={() => setPhotoIndex(idx)}
                />
              ))}
            </div>
          )}
        </div>

        {immersed && !moving && activeCity && (
          <nav className="cmp-progress" aria-label="旅行条件导航">
            <button type="button" onClick={leaveFlow} disabled={isSubmitting}>
              {activeCity.name}
            </button>
            <span />
            <button
              type="button"
              aria-current={step === "dates" ? "step" : undefined}
              disabled={isSubmitting} onClick={() => setStep("dates")}
            >
              {dateSummary}
            </button>
            <span />
            <button
              type="button"
              disabled={isSubmitting || !startDate || !endDate}
              aria-current={step === "companions" ? "step" : undefined}
              onClick={() => setStep("companions")}
            >
              {companion || "同行"}
            </button>
            <span />
            <button
              type="button"
              disabled={isSubmitting || !companion}
              aria-current={step === "preferences" ? "step" : undefined}
              onClick={() => setStep("preferences")}
            >
              偏好
            </button>
            <span />
            <button
              type="button"
              aria-current={step === "commute" ? "step" : undefined}
              disabled={isSubmitting} onClick={() => setStep("commute")}
            >
              出行
            </button>
            <span />
            <button
              type="button"
              aria-current={step === "review" ? "step" : undefined}
              disabled={isSubmitting} onClick={() => setStep("review")}
            >
              确认
            </button>
          </nav>
        )}
      </footer>

      {activeCity && (
        <CinematicPoiPicker
          city={activeCity.name}
          isOpen={poiPickerOpen}
          selectedPois={selectedPois}
          onClose={() => setPoiPickerOpen(false)}
          onSave={setSelectedPois}
        />
      )}
    </div>
  );
}

