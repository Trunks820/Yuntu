import type { TripFormData } from "./form";

/**
 * Guest submit body for POST /api/trip/async.
 * Dates stay off the payload when the traveler has not picked them yet.
 */
export type TripSubmitRequest = Omit<TripFormData, "start_date" | "end_date"> & {
  start_date?: string;
  end_date?: string;
};
