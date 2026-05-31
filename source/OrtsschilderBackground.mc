import Toybox.Application.Storage;
import Toybox.Background;
import Toybox.Communications;
import Toybox.Lang;
import Toybox.Position;
import Toybox.System;
import Toybox.Time;

// Background service — runs every 5 min when phone is connected.
// Priority 1: refresh Ortsschild list from Supabase (bbox query, own infra)
// Priority 2: flush crossing upload queue
// Priority 3: upload pending ride track after activity ends
(:background)
class OrtsschilderBackground extends System.ServiceDelegate {

    private const SUPABASE_URL = "https://slcprtkqkqwgstnyfpus.supabase.co";
    private const SUPABASE_KEY = "sb_publishable_xeoJUHq3BEvCJbHdvgjQsg_yHyvhXUJ";

    private const SK_CROSSINGS = "crossings";
    private const SK_SIGNS     = "signs";

    public function initialize() {
        ServiceDelegate.initialize();
    }

    // Single source of truth for the Supabase REST auth headers.
    private function _authHeaders(withJson as Boolean) as Dictionary {
        var h = {
            "apikey"        => SUPABASE_KEY,
            "Authorization" => "Bearer " + SUPABASE_KEY
        } as Dictionary;
        if (withJson) { h.put("Content-Type", "application/json"); }
        return h;
    }

    public function onTemporalEvent() as Void {
        // Debug: write timestamp so DataField can confirm Background is running
        Storage.setValue("bg_started", Time.now().value());

        try {
            Background.registerForTemporalEvent(
                Time.now().add(new Time.Duration(600))
            );
        } catch (e instanceof Lang.Exception) {}

        // Priority 1: signs — always first
        // First load (empty): fetch immediately.
        // Updates: every 10 min while activity is running.
        var signsData  = Storage.getValue(SK_SIGNS);
        var signsEmpty = !(signsData instanceof Array) || (signsData as Array).size() == 0;
        var lastFetch  = Storage.getValue("signs_ts");
        var firstLoad  = signsEmpty || !(lastFetch instanceof Number);
        var actActive  = Storage.getValue("activity_active");
        var actRunning = (actActive instanceof Boolean) && (actActive as Boolean);
        var needsRefresh = firstLoad ||
            (actRunning && (Time.now().value() - (lastFetch as Number)) > 600);

        if (needsRefresh) {
            var posInfo = Position.getInfo();
            var posObj  = (posInfo != null) ? posInfo.position : null;
            if (posObj != null) {
                var coords = (posObj as Position.Location).toDegrees();
                _fetchSigns((coords[0] as Numeric).toFloat(), (coords[1] as Numeric).toFloat());
                return;
            }
            var stored = Storage.getValue("current_pos");
            if (stored instanceof Array) {
                _fetchSigns((stored as Array)[0] as Float, (stored as Array)[1] as Float);
                return;
            }
            // No GPS yet — fall through to uploads
        }

        // Priority 2: flush crossing queue
        var raw = Storage.getValue(SK_CROSSINGS);
        if (raw instanceof Array && (raw as Array).size() > 0) {
            _uploadCrossing((raw as Array)[0] as Dictionary);
            return;
        }

        // Priority 3: upload ride track after activity ends
        var pendingRide = Storage.getValue("pending_ride");
        if (pendingRide instanceof Dictionary) {
            _uploadRide(pendingRide as Dictionary);
            return;
        }

        Background.exit(null);
    }

    // ─── Crossings ────────────────────────────────────────────────────────────

    private function _uploadCrossing(crossing as Dictionary) as Void {
        Communications.makeWebRequest(
            SUPABASE_URL + "/rest/v1/crossings",
            crossing,
            {
                :method       => Communications.HTTP_REQUEST_METHOD_POST,
                :headers      => _authHeaders(true),
                :responseType => Communications.HTTP_RESPONSE_CONTENT_TYPE_TEXT_PLAIN
            },
            method(:onUploadResponse)
        );
    }

    public function onUploadResponse(code as Number, data as String or Null) as Void {
        if (code == 200 || code == 201) {
            _dequeueFirst();
            // After successful upload, fetch rankings for this crossing
            _fetchRankingsAfterUpload();
        } else {
            Background.exit(null);
        }
    }

    // Fetch server rankings for the crossing just uploaded, store result in Storage.
    // DataField polls "rank_result" every 3 s and displays it.
    private function _fetchRankingsAfterUpload() as Void {
        // Skip the second HTTP call entirely if a result is already present —
        // keeps each Background invocation to a single network round-trip when possible.
        if (Storage.getValue("rank_result") instanceof Dictionary) {
            Background.exit(null); return;
        }

        var crossing = Storage.getValue("rank_crossing");
        if (!(crossing instanceof Dictionary)) { Background.exit(null); return; }
        var cd    = crossing as Dictionary;
        var sidObj = cd.get("sign_id");
        var tsObj  = cd.get("timestamp");
        if (sidObj == null || tsObj == null) { Background.exit(null); return; }

        // Only fetch if the crossing is recent (< 5 min old)
        var ts = (tsObj instanceof Number) ? (tsObj as Number) : 0;
        if (Time.now().value() - ts > 300) { Background.exit(null); return; }

        var sid   = sidObj.toString();
        var tFrom = (ts - 30).toString();
        var tTo   = (ts + 30).toString();

        Communications.makeWebRequest(
            SUPABASE_URL + "/rest/v1/crossings?sign_id=eq." + sid +
                "&timestamp=gte." + tFrom +
                "&timestamp=lte." + tTo +
                "&order=timestamp.asc&limit=50&select=device_id,timestamp",
            null,
            {
                :method       => Communications.HTTP_REQUEST_METHOD_GET,
                :headers      => _authHeaders(false),
                :responseType => Communications.HTTP_RESPONSE_CONTENT_TYPE_JSON
            },
            method(:onRankingsResponse)
        );
    }

    public function onRankingsResponse(code as Number, data as Dictionary or String or Null) as Void {
        if (code == 200) {
            var obj = data as Lang.Object?;
            if (obj instanceof Array) {
                var crossing = Storage.getValue("rank_crossing");
                if (crossing instanceof Dictionary) {
                    var cd       = crossing as Dictionary;
                    var myDevObj = cd.get("device_id");
                    var myTsObj  = cd.get("timestamp");
                    var myDevStr = (myDevObj != null) ? myDevObj.toString() : "";
                    var myTs     = (myTsObj instanceof Number) ? (myTsObj as Number) : 0;

                    var rows = obj as Array;
                    var best = {} as Dictionary;
                    for (var i = 0; i < rows.size(); i++) {
                        var row   = rows[i] as Dictionary;
                        var devO  = row.get("device_id");
                        var tsO   = row.get("timestamp");
                        if (devO == null || tsO == null) { continue; }
                        var devStr = devO.toString();
                        var t = (tsO instanceof Float) ? (tsO as Float).toNumber()
                              : (tsO instanceof Long)  ? (tsO as Long).toNumber()
                              : (tsO as Number);
                        if (!best.hasKey(devStr) || t < (best.get(devStr) as Number)) {
                            best.put(devStr, t);
                        }
                    }

                    var leaderTs  = myTs;
                    var devKeys   = best.keys();
                    for (var i = 0; i < devKeys.size(); i++) {
                        var t = best.get(devKeys[i]) as Number;
                        if (t < leaderTs) { leaderTs = t; }
                    }

                    var groupCutoff = leaderTs + 30;
                    var fasterCnt   = 0;
                    var groupTotal  = 0;
                    for (var i = 0; i < devKeys.size(); i++) {
                        var devK = devKeys[i].toString();
                        var t    = best.get(devK) as Number;
                        if (t > groupCutoff) { continue; }
                        groupTotal++;
                        if (!devK.equals(myDevStr) && t < myTs) { fasterCnt++; }
                    }

                    Storage.setValue("rank_result", {
                        "pos"   => fasterCnt + 1,
                        "total" => groupTotal,
                        "delta" => (myTs - leaderTs).toFloat()
                    } as Dictionary);
                }
            }
        }
        Background.exit(null);
    }

    private function _dequeueFirst() as Void {
        var raw = Storage.getValue(SK_CROSSINGS);
        if (!(raw instanceof Array)) { return; }
        var arr = raw as Array;
        if (arr.size() <= 1) { Storage.setValue(SK_CROSSINGS, [] as Array); return; }
        var tail = new Array<Object>[arr.size() - 1];
        for (var i = 1; i < arr.size(); i++) { tail[i - 1] = arr[i]; }
        Storage.setValue(SK_CROSSINGS, tail);
    }

    // ─── Ride upload ─────────────────────────────────────────────────────────

    private function _uploadRide(ride as Dictionary) as Void {
        Communications.makeWebRequest(
            SUPABASE_URL + "/rest/v1/rides",
            ride,
            {
                :method       => Communications.HTTP_REQUEST_METHOD_POST,
                :headers      => _authHeaders(true),
                :responseType => Communications.HTTP_RESPONSE_CONTENT_TYPE_TEXT_PLAIN
            },
            method(:onRideResponse)
        );
    }

    public function onRideResponse(code as Number, data as String or Null) as Void {
        if (code == 200 || code == 201) {
            Storage.setValue("pending_ride", null);
        }
        Background.exit(null);
    }

    // ─── Sign refresh (Supabase) ──────────────────────────────────────────────
    // Queries our own Supabase signs table (pre-imported from OSM) with a bbox
    // filter. Much faster and more reliable than querying Overpass directly.
    // Import script: scripts/import_signs.py   Update: monthly via GitHub Actions.

    private function _fetchSigns(lat as Float, lon as Float) as Void {
        // Bounding box ~5 km around current position
        var s = (lat - 0.045f).format("%.4f");
        var w = (lon - 0.068f).format("%.4f");
        var n = (lat + 0.045f).format("%.4f");
        var e = (lon + 0.068f).format("%.4f");

        Storage.setValue("bg_fetch_started", 1);

        // Supabase REST: flat JSON array, no parsing overhead, always available
        Communications.makeWebRequest(
            SUPABASE_URL + "/rest/v1/signs" +
                "?lat=gte." + s + "&lat=lte." + n +
                "&lon=gte." + w + "&lon=lte." + e +
                "&select=id,name,lat,lon&limit=50",
            null,
            {
                :method       => Communications.HTTP_REQUEST_METHOD_GET,
                :headers      => _authHeaders(false),
                :responseType => Communications.HTTP_RESPONSE_CONTENT_TYPE_JSON
            },
            method(:onSignsResponse)
        );
    }

    // Supabase returns a flat JSON array: [{id,name,lat,lon}, ...]
    // No two-pass parse needed — the import script already resolved names.
    public function onSignsResponse(code as Number, data as Dictionary or String or Null) as Void {
        // Debug: store response code so DataField can show it
        Storage.setValue("bg_signs_code", code);

        if (code == 200) {
            var obj = data as Lang.Object?;
            if (obj instanceof Array) {
                var arr      = obj as Array;
                var signList = [] as Array;

                for (var i = 0; i < arr.size() && signList.size() < 40; i++) {
                    var elem  = arr[i] as Dictionary;
                    var idV   = elem.get("id");
                    var nameV = elem.get("name");
                    var latV  = elem.get("lat");
                    var lonV  = elem.get("lon");
                    if (idV == null || nameV == null || latV == null || lonV == null) { continue; }
                    var nameStr = nameV.toString();
                    if (nameStr.equals("")) { continue; }
                    signList.add({
                        "id"   => idV.toString(),
                        "name" => nameStr,
                        "lat"  => (latV instanceof Float) ? (latV as Float)
                                                          : (latV as Number).toFloat(),
                        "lon"  => (lonV instanceof Float) ? (lonV as Float)
                                                          : (lonV as Number).toFloat()
                    } as Dictionary);
                }

                // Debug: store how many signs were parsed
                Storage.setValue("bg_signs_count", signList.size());

                if (signList.size() > 0) {
                    Storage.setValue(SK_SIGNS, signList);
                    Storage.setValue("signs_ts", Time.now().value());
                }
            } else {
                // data was not an Array — unexpected format
                Storage.setValue("bg_signs_count", -1);
            }
        }
        Background.exit(null);
    }
}
