"""Venue event lookup tables, transcribed from YARG.Core.

Source of truth: YARG.Core @ 00c62264bc8aef0d05f28a231432c9a23c9a1449
  - YARG.Core/Chart/Venue/VenueLookup.cs
  - YARG.Core/MoonscraperChartParser/IO/Midi/MidIOHelper.cs

These tables decide what a song's lighting actually *is*, so they are copied
verbatim rather than paraphrased. If YARG.Core moves, re-transcribe; don't
hand-patch entries.
"""

from enum import Enum


class VenueType(str, Enum):
    """VenueLookup.Type — which of the five VenueTrack lists an event feeds."""

    LIGHTING = "lighting"
    POST_PROCESSING = "post_processing"
    SINGALONG = "singalong"
    SPOTLIGHT = "spotlight"
    STAGE_EFFECT = "stage_effect"
    CAMERA_CUT = "camera_cut"
    CAMERA_CUT_CONSTRAINT = "camera_cut_constraint"
    UNKNOWN = "unknown"


#: Prefix flags, stripped off the front of a normalized text event.
FLAG_PREFIXES = {"optional"}

LIGHTING_DEFAULT = "default"
CAMERA_DEFAULT = "default"

# ---------------------------------------------------------------------------
# MidIOHelper.VENUE_NOTE_LOOKUP — legacy Rock Band note-number cues.
# Anything outside this table is silently dropped by YARG.
# ---------------------------------------------------------------------------

VENUE_NOTE_LOOKUP: dict[int, tuple[VenueType, str]] = {
    # Post-processing
    110: (VenueType.POST_PROCESSING, "trails_long"),            # Trails
    109: (VenueType.POST_PROCESSING, "scanlines_security"),     # Security camera
    108: (VenueType.POST_PROCESSING, "scanlines_black_white"),  # Black and white
    107: (VenueType.POST_PROCESSING, "scanlines"),              # Scanlines
    106: (VenueType.POST_PROCESSING, "scanlines_blue"),         # Blue tint
    105: (VenueType.POST_PROCESSING, "mirror"),                 # Mirror
    104: (VenueType.POST_PROCESSING, "desaturated_red"),        # Bloom B
    103: (VenueType.POST_PROCESSING, "bloom"),                  # Bloom A
    102: (VenueType.POST_PROCESSING, "choppy_black_white"),     # Photocopy
    101: (VenueType.POST_PROCESSING, "photonegative"),          # Negative
    100: (VenueType.POST_PROCESSING, "silvertone"),             # Silvertone
    99: (VenueType.POST_PROCESSING, "sepiatone"),               # Sepia
    98: (VenueType.POST_PROCESSING, "grainy_film"),             # 16mm
    97: (VenueType.POST_PROCESSING, "polarized_black_white"),   # Contrast A
    96: (VenueType.POST_PROCESSING, "default"),                 # Default
    # Singalong — note there is deliberately no vocals or keys note here
    87: (VenueType.SINGALONG, "guitar"),
    86: (VenueType.SINGALONG, "drums"),
    85: (VenueType.SINGALONG, "bass"),
    # Camera cut constraints (72 is only_close, 73 is no_close — not reversed)
    73: (VenueType.CAMERA_CUT_CONSTRAINT, "no_close"),
    72: (VenueType.CAMERA_CUT_CONSTRAINT, "only_close"),
    71: (VenueType.CAMERA_CUT_CONSTRAINT, "only_far"),
    70: (VenueType.CAMERA_CUT_CONSTRAINT, "no_behind"),
    # Camera cuts
    64: (VenueType.CAMERA_CUT, "directed_vocals"),
    63: (VenueType.CAMERA_CUT, "directed_guitar"),
    62: (VenueType.CAMERA_CUT, "directed_drums"),
    61: (VenueType.CAMERA_CUT, "directed_bass"),
    60: (VenueType.CAMERA_CUT, "random"),
    # Lighting keyframes
    50: (VenueType.LIGHTING, "first"),
    49: (VenueType.LIGHTING, "previous"),
    48: (VenueType.LIGHTING, "next"),
    # Spotlights
    41: (VenueType.SPOTLIGHT, "keys"),
    40: (VenueType.SPOTLIGHT, "vocals"),
    39: (VenueType.SPOTLIGHT, "guitar"),
    38: (VenueType.SPOTLIGHT, "drums"),
    37: (VenueType.SPOTLIGHT, "bass"),
}

# ---------------------------------------------------------------------------
# VenueLookup.VENUE_TEXT_CONVERSION_LOOKUP — exact-match, case sensitive.
# ---------------------------------------------------------------------------

VENUE_TEXT_CONVERSION_LOOKUP: dict[str, tuple[VenueType, str]] = {
    # Lighting keyframe events
    "first": (VenueType.LIGHTING, "first"),
    "next": (VenueType.LIGHTING, "next"),
    "prev": (VenueType.LIGHTING, "previous"),
    # RBN1 equivalents for `lighting (chorus)` and `lighting (verse)`
    "verse": (VenueType.LIGHTING, "verse"),
    "chorus": (VenueType.LIGHTING, "chorus"),
    # Post-processing
    "bloom.pp": (VenueType.POST_PROCESSING, "bloom"),
    "bright.pp": (VenueType.POST_PROCESSING, "bright"),
    "clean_trails.pp": (VenueType.POST_PROCESSING, "trails"),
    "contrast_a.pp": (VenueType.POST_PROCESSING, "polarized_black_white"),
    "desat_blue.pp": (VenueType.POST_PROCESSING, "desaturated_blue"),
    "desat_posterize_trails.pp": (VenueType.POST_PROCESSING, "trails_desaturated"),
    "film_contrast.pp": (VenueType.POST_PROCESSING, "contrast"),
    "film_b+w.pp": (VenueType.POST_PROCESSING, "black_white"),
    "film_sepia_ink.pp": (VenueType.POST_PROCESSING, "sepiatone"),
    "film_silvertone.pp": (VenueType.POST_PROCESSING, "silvertone"),
    "film_contrast_red.pp": (VenueType.POST_PROCESSING, "contrast_red"),
    "film_contrast_green.pp": (VenueType.POST_PROCESSING, "contrast_green"),
    "film_contrast_blue.pp": (VenueType.POST_PROCESSING, "contrast_blue"),
    "film_16mm.pp": (VenueType.POST_PROCESSING, "grainy_film"),
    "film_blue_filter.pp": (VenueType.POST_PROCESSING, "scanlines_blue"),
    "flicker_trails.pp": (VenueType.POST_PROCESSING, "trails_flickery"),
    "horror_movie_special.pp": (VenueType.POST_PROCESSING, "photonegative_red_black"),
    "photocopy.pp": (VenueType.POST_PROCESSING, "choppy_black_white"),
    "photo_negative.pp": (VenueType.POST_PROCESSING, "photonegative"),
    "posterize.pp": (VenueType.POST_PROCESSING, "posterize"),
    "ProFilm_a.pp": (VenueType.POST_PROCESSING, "default"),
    "ProFilm_b.pp": (VenueType.POST_PROCESSING, "desaturated_red"),
    "ProFilm_mirror_a.pp": (VenueType.POST_PROCESSING, "mirror"),
    "ProFilm_psychedelic_blue_red.pp": (VenueType.POST_PROCESSING, "polarized_red_blue"),
    "shitty_tv.pp": (VenueType.POST_PROCESSING, "grainy_chromatic_abberation"),
    "space_woosh.pp": (VenueType.POST_PROCESSING, "trails_spacey"),
    "video_a.pp": (VenueType.POST_PROCESSING, "scanlines"),
    "video_bw.pp": (VenueType.POST_PROCESSING, "scanlines_black_white"),
    "video_security.pp": (VenueType.POST_PROCESSING, "scanlines_security"),
    "video_trails.pp": (VenueType.POST_PROCESSING, "trails_long"),
    # Stage effects
    "bonusfx": (VenueType.STAGE_EFFECT, "bonus_fx"),
    "bonusfx_optional": (VenueType.STAGE_EFFECT, "optional bonus_fx"),
    "FogOn": (VenueType.STAGE_EFFECT, "fog_on"),
    "FogOff": (VenueType.STAGE_EFFECT, "fog_off"),
}

# ---------------------------------------------------------------------------
# Regex-tier lookups. Empty string is deliberately absent from the lighting
# table — `lighting ()` falls through to LIGHTING_DEFAULT.
# ---------------------------------------------------------------------------

VENUE_LIGHTING_CONVERSION_LOOKUP: dict[str, str] = {
    # Keyframed
    "chorus": "chorus",
    "dischord": "dischord",
    "manual_cool": "cool_manual",
    "manual_warm": "warm_manual",
    "stomp": "stomp",
    "verse": "verse",
    # Automatic
    "blackout_fast": "blackout_fast",
    "blackout_slow": "blackout_slow",
    "blackout_spot": "blackout_spotlight",
    "bre": "big_rock_ending",
    "flare_fast": "flare_fast",
    "flare_slow": "flare_slow",
    "frenzy": "frenzy",
    "harmony": "harmony",
    "intro": "intro",
    "loop_cool": "cool_automatic",
    "loop_warm": "warm_automatic",
    "searchlights": "searchlights",
    "silhouettes": "silhouettes",
    "silhouettes_spot": "silhouettes_spotlight",
    "strobe_fast": "strobe_fast",
    "strobe_slow": "strobe_slow",
    "sweep": "sweep",
}

VENUE_DIRECTED_CUT_LOOKUP: dict[str, str] = {
    "directed_guitar": "directed_guitar",
    "directed_bass": "directed_bass",
    "directed_drums": "directed_drums",
    "directed_vocals": "directed_vocals",
    "directed_stagedive": "directed_stagedive",
    "directed_crowdsurf": "directed_crowdsurf",
    "directed_all": "directed_all",
    "directed_bre": "directed_bre",
    "directed_brej": "directed_brej",
    "directed_guitar_cam": "directed_guitar_cam",
    "directed_bass_cam": "directed_bass_cam",
    "directed_drums_kd": "directed_drums_kd",
    "directed_drums_lt": "directed_drums_lt",
    "directed_drums_np": "directed_drums_np",
    "directed_crowd_g": "directed_crowd_g",
    "directed_crowd_b": "directed_crowd_b",
    # Both of these collapse onto the same value upstream
    "directed_crowd_pnt": "directed_drums_pnt",
    "directed_drums_pnt": "directed_drums_pnt",
    "directed_duo_drums": "directed_duo_drums",
    "directed_guitar_cls": "directed_guitar_cls",
    "directed_bass_cls": "directed_bass_cls",
    "directed_vocals_cam": "directed_vocals_cam",
    "directed_vocals_cls": "directed_vocals_cls",
    "directed_all_cam": "directed_all_cam",
    "directed_all_lt": "directed_all_lt",
    "directed_all_yeah": "directed_all_yeah",
    "default": "default",
    "directed_guitar_np": "directed_guitar_np",
    "directed_bass_np": "directed_bass_np",
    "directed_vocals_np": "directed_vocals_np",
}

VENUE_CAMERA_CUT_LOOKUP: dict[str, str] = {
    # Four shots
    "all_behind": "all_behind",
    "all_far": "all_far",
    "all_near": "all_near",
    # Three shots (no drums)
    "front_behind": "front_behind",
    "front_near": "front_near",
    # One character standard shots
    "d_behind": "d_behind",
    "d_near": "d_near",
    "v_behind": "v_behind",
    "v_near": "v_near",
    "b_behind": "b_behind",
    "b_near": "b_near",
    "g_behind": "g_behind",
    "g_near": "g_near",
    "k_behind": "k_behind",
    "k_near": "k_near",
    # One character closeups
    "d_closeup_hand": "d_closeup_hand",
    "d_closeup_head": "d_closeup_head",
    "v_closeup": "v_closeup",
    "b_closeup_hand": "b_closeup_hand",
    "b_closeup_head": "b_closeup_head",
    "g_closeup_hand": "g_closeup_hand",
    "g_closeup_head": "g_closeup_head",
    "k_closeup_hand": "k_closeup_hand",
    "k_closeup_head": "k_closeup_head",
    # Two character shots
    "dv_near": "dv_near",
    "bd_near": "bd_near",
    "dg_near": "dg_near",
    "bv_behind": "bv_behind",
    "bv_near": "bv_near",
    "gv_behind": "gv_behind",
    "gv_near": "gv_near",
    "kv_behind": "kv_behind",
    "kv_near": "kv_near",
    "bg_behind": "bg_behind",
    "bg_near": "bg_near",
    "bk_behind": "bk_behind",
    "bk_near": "bk_near",
    "gk_behind": "gk_behind",
    "gk_near": "gk_near",
}

# ---------------------------------------------------------------------------
# Final normalization: text -> concrete YARG type. Anything a chart can produce
# that is NOT in these sets is dropped by MoonSongLoader.LoadVenueTrack, so it
# must not count toward "this song has authored lighting".
# ---------------------------------------------------------------------------

#: VenueLookup.LightingLookup keys. StrobeFastest/StrobeMedium/StrobeOff/Menu/
#: Score/NoCue exist as LightingType members but have no lookup entry, so they
#: can never originate from a chart or milo.
LIGHTING_TYPES = {
    "default", "dischord", "chorus", "cool_manual", "stomp", "verse",
    "warm_manual", "big_rock_ending", "blackout_fast", "blackout_slow",
    "blackout_spotlight", "cool_automatic", "flare_fast", "flare_slow",
    "frenzy", "intro", "harmony", "silhouettes", "silhouettes_spotlight",
    "searchlights", "strobe_fast", "strobe_slow", "sweep", "warm_automatic",
    "first", "next", "previous",
}

#: Lighting cues YARG treats as manually keyframed (VenueAutoGenerationPreset
#: emits KeyframeNext for these).
LIGHTING_MANUAL = {
    "default", "dischord", "chorus", "cool_manual", "stomp", "verse",
    "warm_manual",
}

#: VenueLookup.PostProcessLookup keys.
POST_PROCESSING_TYPES = {
    "default", "bloom", "bright", "contrast", "mirror", "photonegative",
    "posterize", "black_white", "sepiatone", "silvertone",
    "choppy_black_white", "photonegative_red_black", "polarized_black_white",
    "polarized_red_blue", "desaturated_red", "desaturated_blue",
    "contrast_red", "contrast_green", "contrast_blue", "grainy_film",
    "grainy_chromatic_abberation", "scanlines", "scanlines_black_white",
    "scanlines_blue", "scanlines_security", "trails", "trails_long",
    "trails_desaturated", "trails_flickery", "trails_spacey",
}

#: VenueLookup.StageEffectLookup keys.
STAGE_EFFECT_TYPES = {"bonus_fx", "fog_on", "fog_off"}

#: VenueLookup.PerformerLookup keys.
PERFORMERS = {"guitar", "bass", "drums", "vocals", "keys"}

#: VenueLookup.CameraCutConstraintLookup keys.
CAMERA_CUT_CONSTRAINTS = {"no_behind", "only_far", "no_close", "only_close"}

#: VenueLookup.CameraCutSubjectLookup — camera cut text -> coarse subject.
CAMERA_CUT_SUBJECTS: dict[str, str] = {
    "directed_all": "Stage",
    "directed_all_cam": "Stage",
    "directed_all_lt": "Stage",
    "directed_all_yeah": "Stage",
    "directed_bre": "Stage",
    "directed_brej": "Stage",
    "directed_guitar": "Guitar",
    "directed_guitar_cam": "Guitar",
    "directed_guitar_cls": "GuitarCloseup",
    "directed_guitar_np": "Guitar",
    "directed_crowd_g": "Stage",
    "directed_bass": "Bass",
    "directed_bass_cam": "Bass",
    "directed_bass_cls": "BassCloseup",
    "directed_bass_np": "Bass",
    "directed_crowd_b": "Bass",
    "directed_drums": "Drums",
    "directed_drums_kd": "DrumsKick",
    "directed_drums_lt": "Drums",
    "directed_drums_np": "Drums",
    "directed_duo_drums": "Drums",
    "directed_drums_pnt": "Stage",
    "directed_stagedive": "Stage",
    "directed_crowdsurf": "Stage",
    "directed_vocals": "Vocals",
    "directed_vocals_cam": "Vocals",
    "directed_vocals_cls": "Vocals",
    "directed_vocals_np": "Vocals",
    "default": "Stage",
    "random": "Random",
    # RBN2 text events
    "all_behind": "AllBehind",
    "all_far": "AllFar",
    "all_near": "AllNear",
    "front_behind": "BehindNoDrum",
    "front_near": "NearNoDrum",
    "d_behind": "DrumsBehind",
    "d_near": "Drums",
    "v_behind": "VocalsBehind",
    "v_near": "Vocals",
    "b_behind": "BassBehind",
    "b_near": "Bass",
    "g_behind": "GuitarBehind",
    "g_near": "Guitar",
    "k_behind": "KeysBehind",
    "k_near": "Keys",
    "d_closeup_hand": "DrumsCloseupHand",
    "d_closeup_head": "DrumsCloseupHead",
    "v_closeup": "VocalsCloseup",
    "b_closeup_hand": "BassCloseup",
    "b_closeup_head": "BassCloseupHead",
    "g_closeup_hand": "GuitarCloseup",
    "g_closeup_head": "GuitarCloseupHead",
    "k_closeup_hand": "KeysCloseupHand",
    "k_closeup_head": "KeysCloseupHead",
    "dv_near": "DrumsVocals",
    "bd_near": "BassDrums",
    "dg_near": "DrumsGuitar",
    "bv_behind": "BassVocalsBehind",
    "bv_near": "BassVocals",
    "gv_behind": "GuitarVocalsBehind",
    "gv_near": "GuitarVocals",
    "kv_behind": "KeysVocalsBehind",
    "kv_near": "KeysVocals",
    "bg_behind": "BassGuitarBehind",
    "bg_near": "BassGuitar",
    "bk_behind": "BassKeysBehind",
    "bk_near": "BassKeys",
    "gk_behind": "GuitarKeysBehind",
    "gk_near": "GuitarKeys",
}
