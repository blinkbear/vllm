"""A block manager that manages token blocks."""
import math
from abc import ABC, abstractmethod
from itertools import count, takewhile
from os.path import commonprefix
from typing import Dict, List, Optional
from typing import Sequence as GenericSequence
from typing import Set, Tuple

from vllm.block import BlockTable, PhysicalTokenBlock
from vllm.core.block.utils import check_no_caching_or_swa_for_blockmgr_encdec
from vllm.core.evictor_v1 import EvictionPolicy, Evictor, make_evictor
from vllm.core.interfaces import AllocStatus, BlockSpaceManager
from vllm.logger import init_logger
from vllm.sequence import Sequence, SequenceGroup, SequenceStatus, SequenceType
from vllm.utils import Device

logger = init_logger(__name__)


class BlockAllocatorBase(ABC):
    """Manages free physical token blocks for a device.

    The allocator maintains a list of free blocks and allocates a block when
    requested. When a block is freed, its reference count is decremented. If
    the reference count becomes zero, the block is added back to the free list.
    """

    @abstractmethod
    def __init__(self,
                 device: Device,
                 block_size: int,
                 num_blocks: int,
                 num_shared_blocks: int = 0,
                 eviction_policy: EvictionPolicy = EvictionPolicy.LRU):
        pass

    @abstractmethod
    def allocate(self,
                 block_hash: Optional[int] = None,
                 num_hashed_tokens: int = 0, 
                 block_type:str ='normal') -> PhysicalTokenBlock:
        pass

    @abstractmethod
    def free(self, block: PhysicalTokenBlock,block_type:str ='normal') -> None:
        pass

    @abstractmethod
    def get_num_free_blocks(self,block_type:str ='normal') -> int:
        pass

    @abstractmethod
    def get_num_total_blocks(self,block_type:str ='normal') -> int:
        pass

    @abstractmethod
    def contains_block(self, block_hash: int) -> bool:
        pass

    @abstractmethod
    def update_hash(self, block_hash: int, block: PhysicalTokenBlock):
        pass
    
    @abstractmethod
    def merge_tables(self):
        pass
    
    @abstractmethod
    def split_tables(self, num_shared_blocks)-> int:
        pass


class CachedBlockAllocator(BlockAllocatorBase):
    """Manages free physical token blocks for a device.

    The allocator maintains a list of free blocks and allocates a block when
    requested. When a block is freed, its reference count is decremented. If
    the reference count becomes zero, the block is added back to the free list.
    """

    def __init__(self,
                 device: Device,
                 block_size: int,
                 num_blocks: int,
                 eviction_policy: EvictionPolicy = EvictionPolicy.LRU) -> None:
        self.device = device
        self.block_size = block_size
        self.num_blocks = num_blocks

        self.current_num_blocks = 0
        self.cached_blocks: Dict[int, PhysicalTokenBlock] = {}

        self.evictor: Evictor = make_evictor(eviction_policy)

        self.default_hash_ctr = count()

    def allocate_block(self, block_hash: int,
                       num_hashed_tokens: int, block_type='normal') -> PhysicalTokenBlock:
        if self.current_num_blocks == self.num_blocks:
            block = self.evictor.evict()
            block.block_hash = block_hash
            block.num_hashed_tokens = num_hashed_tokens
            return block
        block = PhysicalTokenBlock(device=self.device,
                                   block_number=self.current_num_blocks,
                                   block_size=self.block_size,
                                   block_hash=block_hash,
                                   num_hashed_tokens=num_hashed_tokens)
        self.current_num_blocks += 1
        return block

    def allocate(self,
                 block_hash: Optional[int] = None,
                 num_hashed_tokens: int = 0, block_type='normal') -> PhysicalTokenBlock:
        if block_hash is None:
            block_hash = next(self.default_hash_ctr)
        if block_hash in self.evictor:
            assert block_hash not in self.cached_blocks
            block = self.evictor.remove(block_hash)
            assert block.ref_count == 0
            self.cached_blocks[block_hash] = block
            block.ref_count += 1
            assert block.block_hash == block_hash
            return block
        if block_hash not in self.cached_blocks:
            self.cached_blocks[block_hash] = self.allocate_block(
                block_hash, num_hashed_tokens)
        block = self.cached_blocks[block_hash]
        assert block.block_hash == block_hash
        block.ref_count += 1
        return block

    def free(self, block: PhysicalTokenBlock, block_type='normal') -> None:
        if block.ref_count == 0:
            raise ValueError(f"Double free! {block} is already freed.")
        block.ref_count -= 1
        if block.ref_count == 0:
            assert block.block_hash not in self.evictor
            self.evictor.add(block)
            # Remove the block from the cached_blocks
            del self.cached_blocks[block.block_hash]

    def get_num_free_blocks(self, block_type='normal') -> int:
        return (self.num_blocks - self.current_num_blocks +
                self.evictor.num_blocks)

    def get_num_total_blocks(self, block_type='normal') -> int:
        return self.num_blocks

    def contains_block(self, block_hash: int) -> bool:
        return block_hash in self.cached_blocks or block_hash in self.evictor

    def update_hash(self, block_hash: int, block: PhysicalTokenBlock):
        # Update the hash of block and the cached_blocks dictionary.
        assert not self.contains_block(block_hash)
        old_hash = block.block_hash
        block.block_hash = block_hash
        del self.cached_blocks[old_hash]
        self.cached_blocks[block_hash] = block
    def merge_tables(self):
        raise NotImplementedError("Invalid codepath for cached block allocator.")

    def split_tables(self, num_shared_blocks):
        raise NotImplementedError("Invalid codepath for cached block allocator.")


class UncachedBlockAllocator(BlockAllocatorBase):
    """Manages free physical token blocks for a device.

    The allocator maintains a list of free blocks and allocates a block when
    requested. When a block is freed, its reference count is decremented. If
    the reference count becomes zero, the block is added back to the free list.
    """

    def __init__(
        self,
        device: Device,
        block_size: int,
        num_blocks: int,
        num_shared_blocks: int,
    ) -> None:
        self.device = device
        self.block_size = block_size
        self.num_blocks = num_blocks
        self.num_shared_blocks = 0 
        self.free_shared_blocks: BlockTable = []
        # Initialize the free blocks.
        self.free_blocks: BlockTable = []
        self.total_blocks: BlockTable = []
        for i in range(num_blocks+num_shared_blocks):
            block = PhysicalTokenBlock(device=device,
                                       block_number=i,
                                       block_size=block_size,
                                       block_hash=-1,
                                       num_hashed_tokens=0)
            self.total_blocks.append(block)

        self.free_blocks = self.total_blocks[:num_blocks]        
        self.free_shared_blocks = self.total_blocks[num_blocks:]


    def allocate(self,
                 block_hash: Optional[int] = None,
                 num_hashed_tokens: int = 0,
                 block_type='normal') -> PhysicalTokenBlock:
        if block_type=='normal':
            if not self.free_blocks:
                raise ValueError("Out of memory! No free blocks are available.")
            block = self.free_blocks.pop()
            block.ref_count = 1
        elif block_type=='shared':
            if not self.free_shared_blocks:
                raise ValueError("Out of shared memory! No free blocks are available.")
            block = self.free_shared_blocks.pop()
            block.ref_count = 1
        else:
            raise ValueError("Unknown block type")
        return block

    def free(self, block: PhysicalTokenBlock, block_type='normal') -> None:
        if block.ref_count == 0:
            raise ValueError(f"Double free! {block} is already freed.")
        block.ref_count -= 1
        if block.ref_count == 0:
            if block_type=='normal':
                self.free_blocks.append(block)
            elif block_type=='shared':
                self.free_shared_blocks.append(block)
            else:
                raise ValueError("Unknown block type")

    def get_num_free_blocks(self, block_type='normal') -> int:
        if block_type=='normal':
            return len(self.free_blocks)
        elif block_type=='shared':
            return len(self.free_shared_blocks)
        else:
            raise ValueError("Unknown block type")

    def get_num_total_blocks(self, block_type='normal') -> int:
        if block_type=='normal':
            return self.num_blocks
        elif block_type=='shared':
            return self.num_shared_blocks
        else:
            raise ValueError("Unknown block type")

    def contains_block(self, block_hash: int) -> bool:
        raise NotImplementedError(
            "Invalid codepath for uncached block allocator.")

    def update_hash(self, block_hash: int, block: PhysicalTokenBlock):
        raise NotImplementedError(
            "Invalid codepath for uncached block allocator.")
        
    def merge_tables(self):
        self.free_blocks.extend(self.free_shared_blocks)
        self.num_shared_blocks -= len(self.free_shared_blocks)
        self.free_shared_blocks = []
        
    def split_tables(self, num_shared_blocks)-> int:
        for block in self.free_blocks:
            if self.num_shared_blocks >= num_shared_blocks:
                break
            self.free_shared_blocks.append(block)
            self.free_blocks.remove(block)
            self.num_shared_blocks += 1   
        return len(self.free_shared_blocks) 
            

class BlockSpaceManagerV1(BlockSpaceManager):
    """Manages the mapping between logical and physical token blocks."""

    def __init__(
        self,
        block_size: int,
        num_gpu_blocks: int,
        num_cpu_blocks: int,
        num_shared_blocks: int,
        watermark: float = 0.00,
        sliding_window: Optional[int] = None,
        enable_caching: bool = False,
    ) -> None:
        self.block_size = block_size
        self.num_total_gpu_blocks = num_gpu_blocks
        self.num_total_cpu_blocks = num_cpu_blocks
        self.num_shared_blocks = num_shared_blocks
        if enable_caching and sliding_window is not None:
            raise NotImplementedError(
                "Sliding window is not allowed with prefix caching enabled!")

        self.block_sliding_window = None
        if sliding_window is not None:
            # Round up to nearest block size to regularize sliding window
            # allocation sizes.
            self.block_sliding_window = math.ceil(sliding_window / block_size)

        self.watermark = watermark
        assert watermark >= 0.0

        self.enable_caching = enable_caching

        self.watermark_blocks = int(watermark * num_gpu_blocks)

        if self.enable_caching:
            logger.info("Automatic prefix caching is enabled.")
            self.gpu_allocator: BlockAllocatorBase = CachedBlockAllocator(
                Device.GPU, block_size, num_gpu_blocks)
            self.cpu_allocator: BlockAllocatorBase = CachedBlockAllocator(
                Device.CPU, block_size, num_cpu_blocks)
        else:
            self.gpu_allocator = UncachedBlockAllocator(
                Device.GPU, block_size, num_gpu_blocks, num_shared_blocks)
            self.cpu_allocator = UncachedBlockAllocator(
                Device.CPU, block_size, num_cpu_blocks, 0)
        # Mapping: seq_id -> BlockTable.
        self.block_tables: Dict[int, BlockTable] = {}
        self.shared_block_tables: Dict[int, BlockTable] = {}
        self.gpu_block_capacity = self.num_total_gpu_blocks - self.watermark_blocks - self.num_shared_blocks
        # self.block_device_tables: Dict[int, Dict[int, Device]]={}
        # Mapping: req_id -> BlockTable
        # Note that each SequenceGroup has a unique
        # request ID
        self.cross_block_tables: Dict[str, BlockTable] = {}

    def _get_seq_num_required_blocks(self, seq: Sequence) -> int:
        return 0 if seq is None else seq.n_blocks


    def can_allocate(self, seq_group: SequenceGroup, shared=False) -> AllocStatus:
        # FIXME(woosuk): Here we assume that all sequences in the group share
        # the same prompt. This may not be true for preempted sequences.

        check_no_caching_or_swa_for_blockmgr_encdec(self, seq_group)

        self_num_required_blocks = self._get_seq_num_required_blocks(
            seq_group.get_seqs(status=SequenceStatus.WAITING)[0])
        cross_num_required_blocks = self._get_seq_num_required_blocks(
            seq_group.get_encoder_seq())
        num_required_blocks = self_num_required_blocks + \
                              cross_num_required_blocks

        if self.block_sliding_window is not None:

            num_required_blocks = min(num_required_blocks,
                                      self.block_sliding_window)
        if shared:
            num_free_gpu_blocks = self.gpu_allocator.get_num_free_blocks(block_type='shared')
            if (num_free_gpu_blocks - num_required_blocks < 0):
                return AllocStatus.LATER
            else:
                return AllocStatus.OK
        else: 
            num_free_gpu_blocks = self.gpu_allocator.get_num_free_blocks()
        # Use watermark to avoid frequent cache eviction.
            if (self.gpu_block_capacity - num_required_blocks <
                    0):
                return AllocStatus.NEVER
            if num_free_gpu_blocks - num_required_blocks >= self.watermark_blocks:
                return AllocStatus.OK
            else:
                return AllocStatus.LATER

    def can_allocate_infer(self, required_block_size: int) -> bool:
        num_total_gpu_blocks = self.gpu_allocator.get_num_total_blocks()
        return num_total_gpu_blocks - required_block_size >= self.watermark_blocks


    def _allocate_sequence(self, \
                           seq: Sequence, \
                           ref_count: int, \
                           shared: bool=False, \
                           is_encoder_decoder: bool = True) -> BlockTable:
        # Allocate new physical token blocks that will store the prompt tokens.
        num_prompt_blocks = seq.n_blocks

        block_table: BlockTable = []
        # block_device_table: Dict[BlockTable, Device] = {}
        for logical_idx in range(num_prompt_blocks):
            if (self.block_sliding_window is not None
                    and logical_idx >= self.block_sliding_window):
                block = block_table[logical_idx % self.block_sliding_window]
                # Set the reference counts of the token blocks.
                block.ref_count = ref_count
            elif not is_encoder_decoder and self.enable_caching:
                block = self.gpu_allocator.allocate(
                    seq.hash_of_block(logical_idx),
                    seq.num_hashed_tokens_of_block(logical_idx))
            else:
                block = self.gpu_allocator.allocate(block_type='shared') if shared else self.gpu_allocator.allocate()
                # Set the reference counts of the token blocks.
                block.ref_count = ref_count
            block_table.append(block)
            # block_device_table[block.block_number] = Device.GPU

        return block_table

    def allocate(self, seq_group: SequenceGroup, shared=False) -> None:
        is_encoder_decoder = seq_group.is_encoder_decoder()
        check_no_caching_or_swa_for_blockmgr_encdec(self, seq_group)

        # Allocate decoder sequences
        #
        # NOTE: Here we assume that all sequences in the group have the same
        # decoder prompt.
        seq = seq_group.get_seqs(status=SequenceStatus.WAITING)[0]
        block_table: BlockTable = \
            self._allocate_sequence(seq,
                                    seq_group.num_seqs(),
                                    shared,
                                    is_encoder_decoder)

        # Assign the self-attention block tables for each sequence.
        for seq in seq_group.get_seqs(status=SequenceStatus.WAITING):
            if shared:
                self.shared_block_tables[seq.seq_id] = block_table.copy()
            else:
                self.block_tables[seq.seq_id] = block_table.copy()
            # self.block_device_tables[seq.seq_id] = {
            #     block.block_number: Device.GPU for block in block_table}

        # Allocate encoder sequence
        if is_encoder_decoder:
            # A SequenceGroup has only a single encoder sequence (at most),
            # thus allocate with a ref count of 1
            block_table = self._allocate_sequence(seq_group.get_encoder_seq(),
                                                  1, shared=False, is_encoder_decoder=is_encoder_decoder)
            # Assign the cross-attention block table for the SequenceGroup.
            self.cross_block_tables[seq_group.request_id] = block_table

        # Allocate encoder sequence
        if is_encoder_decoder:
            # A SequenceGroup has only a single encoder sequence (at most),
            # thus allocate with a ref count of 1
            block_table = self._allocate_sequence(seq_group.get_encoder_seq(),
                                                  1, shared=False, is_encoder_decoder=is_encoder_decoder)
            # Assign the cross-attention block table for the SequenceGroup.
            self.cross_block_tables[seq_group.request_id] = block_table
    def fake_allocate(self, seq_group: SequenceGroup) -> None:
        seq = seq_group.get_seqs(status=SequenceStatus.WAITING)[0]

        # Allocate new physical token blocks that will store the prompt tokens.
        num_prompt_blocks = seq.n_blocks

        block_table: BlockTable = []

        for logical_idx in range(num_prompt_blocks):
            block_table.append(-1)

        # Assign the block table for each sequence.
        for seq in seq_group.get_seqs(status=SequenceStatus.WAITING):
                self.block_tables[seq.seq_id] = block_table.copy()
    
    def get_fake_block_table_and_delete(self, seq: Sequence) -> List[int]:
        block_table = self.block_tables[seq.seq_id]
        ret = [-1 for block in block_table]
        del self.block_tables[seq.seq_id]
        return ret
    def can_append_slots(self,
                         seq_group: SequenceGroup,
                         num_lookahead_slots: int = 0) -> bool:
        assert (num_lookahead_slots == 0
                ), "lookahead allocation not supported in BlockSpaceManagerV1"

        # Simple heuristic: If there is at least one free block
        # for each sequence, we can append.
        seq_type = seq_group.get_seqs(status=SequenceStatus.RUNNING)[0].get_seq_type()
        if seq_type == SequenceType.NORMAL:
            num_free_gpu_blocks = self.gpu_allocator.get_num_free_blocks()
            num_seqs = seq_group.num_seqs(status=SequenceStatus.RUNNING)
        elif seq_type == SequenceType.TEMP:
            num_free_gpu_blocks = self.gpu_allocator.get_num_free_blocks(block_type='shared')
            num_seqs = seq_group.num_seqs(status=SequenceStatus.RUNNING)
        else:
            raise ValueError("Unknown block type")

        return num_seqs <= num_free_gpu_blocks

    def _promote_last_block(
        self,
        seq: Sequence,
        last_block: PhysicalTokenBlock,
    ) -> PhysicalTokenBlock:
        assert self.enable_caching

        # Compute a new hash for the block so that it can be shared by other
        # Sequences
        new_hash = seq.hash_of_block(seq.n_blocks - 1)

        # if new_hash is already in the cached table, then free last_block
        # and return the cached version
        if self.gpu_allocator.contains_block(new_hash):
            self.gpu_allocator.free(last_block)
            return self.gpu_allocator.allocate(new_hash)
        else:
            self.gpu_allocator.update_hash(new_hash, last_block)
            return last_block

    def _is_last_block_full(
        self,
        seq: Sequence,
    ) -> bool:
        token_ids_len = seq.data.get_len()
        return token_ids_len > 0 and token_ids_len % seq.block_size == 0

    def _maybe_promote_last_block(
        self,
        seq: Sequence,
        last_block: PhysicalTokenBlock,
    ) -> PhysicalTokenBlock:
        if self._is_last_block_full(seq):
            return self._promote_last_block(seq, last_block)
        else:
            return last_block

    def _allocate_last_physical_block(
        self,
        seq: Sequence,
    ) -> PhysicalTokenBlock:
        # Called before a new block is appended.
        # This is in charge of allocating a new physical block (to be appended).

        # None if the last block is not full. Otherwise, we set it to the
        # content hash.
        seq_type = seq.get_seq_type()
        if not self.enable_caching:
            if seq_type == SequenceType.TEMP:
                return self.gpu_allocator.allocate(block_type='shared')
            elif seq_type == SequenceType.NORMAL:
                return self.gpu_allocator.allocate()
        block_hash: Optional[int] = None
        n_blocks = seq.n_blocks
        if (self._is_last_block_full(seq)):
            block_hash = seq.hash_of_block(n_blocks - 1)
        num_hashed_tokens = seq.num_hashed_tokens_of_block(n_blocks - 1)

        # num_hashed_tokens is used to compute future hashes
        # (e.g. in the hashing function, it is used to ask the sequence for
        # prefix tokens)
        new_block = self.gpu_allocator.allocate(
            block_hash, 
            num_hashed_tokens,  
            block_type='shared' if seq_type == SequenceType.TEMP else None)

        # If the block has is None, then the block is not full.
        # If the block is not full, then we expect it to have a refcount of 1.
        if block_hash is None:
            assert new_block.ref_count == 1
        return new_block

    def append_slots(
        self,
        seq: Sequence,
        num_lookahead_slots: int = 0,
    ) -> List[Tuple[int, int]]:
        """Allocate a physical slot for a new token."""
        seq_type = seq.get_seq_type()
        n_blocks = seq.n_blocks
        if seq_type == SequenceType.TEMP:
            block_table = self.shared_block_tables[seq.seq_id]
        else:
            block_table = self.block_tables[seq.seq_id]
        # If we need to allocate a new physical block
        if len(block_table) < n_blocks:
            # Currently this code only supports adding one physical block
            assert len(block_table) == n_blocks - 1

            if (self.block_sliding_window
                    and len(block_table) >= self.block_sliding_window):
                # reuse a block
                block_table.append(block_table[len(block_table) %
                                               self.block_sliding_window])
            else:
                # The sequence hash a new logical block.
                # Allocate a new physical block.
                new_block = self._allocate_last_physical_block(seq)
                # block_device_table[new_block.block_number]=Device.GPU
                block_table.append(new_block)
                return []

        # We want to append the token to the last physical block.
        last_block = block_table[-1]

        assert last_block.device == Device.GPU, f"{seq}"
        if last_block.ref_count == 1:
            # Not shared with other sequences. Appendable.
            if self.enable_caching:
                # If the last block is now complete, we may reuse an old block
                # to save memory.
                maybe_new_block = self._maybe_promote_last_block(
                    seq, last_block)
                block_table[-1] = maybe_new_block
                # block_device_table[maybe_new_block.block_number]=Device.GPU
                # del block_device_table[last_block.block_number]
            return []
        else:
            # The last block is shared with other sequences.
            # Copy on Write: Allocate a new block and copy the tokens.
            new_block = self._allocate_last_physical_block(seq)
            # block_device_table[new_block.block_number]=Device.GPU

            block_table[-1] = new_block
            if seq_type == SequenceType.TEMP:
                self.gpu_allocator.free(last_block,block_type='shared')
            else:
                self.gpu_allocator.free(last_block)
            # del block_device_table[last_block.block_number]
            return [(last_block.block_number, new_block.block_number)]

    def fork(self, parent_seq: Sequence, child_seq: Sequence) -> None:
        # NOTE: fork does not allocate a new physical block.
        # Thus, it is always safe from OOM.
        if parent_seq.seq_id not in self.block_tables:
            # Parent sequence has either been freed or never existed.
            return
        src_block_table = self.block_tables[parent_seq.seq_id]
        self.block_tables[child_seq.seq_id] = src_block_table.copy()
        # When using a sliding window, blocks will be eventually reused.
        # In this case the block tables will contain repeated blocks.
        # When forking, we must make sure that each block's `ref_count`
        # is only incremented by one, so we deduplicate them by wrapping
        # them in a set.
        for block in set(src_block_table):
            block.ref_count += 1

    def _get_physical_blocks(
            self, seq_group: SequenceGroup) -> List[PhysicalTokenBlock]:

        # NOTE: Here, we assume that the physical blocks are only shared by
        # the sequences in the same group.
        request_id = seq_group.request_id
        blocks: Set[PhysicalTokenBlock] = set()
        for seq in seq_group.get_seqs():
            if seq.is_finished():
                continue
            blocks.update(self.block_tables[seq.seq_id])
        # Cross-attention blocks
        if seq_group.is_encoder_decoder():
            blocks.update(self.cross_block_tables[request_id])
        return list(blocks)

    def can_swap_in(self,
                    seq_group: SequenceGroup,
                    num_lookahead_slots: int = 0) -> AllocStatus:
        assert (num_lookahead_slots == 0
                ), "BlockSpaceManagerV1 does not support lookahead allocation"

        blocks = self._get_physical_blocks(seq_group)
        num_swapped_seqs = seq_group.num_seqs(status=SequenceStatus.SWAPPED) + \
                           seq_group.num_seqs(status=SequenceStatus.PARTIAL_SWAPPED)
        if seq_group.is_encoder_decoder():
            num_swapped_seqs += 1
        num_free_blocks = self.gpu_allocator.get_num_free_blocks()
        # NOTE: Conservatively, we assume that every sequence will allocate
        # at least one free block right after the swap-in.
        # NOTE: This should match the logic in can_append_slot().
        num_required_blocks = len(blocks) + num_swapped_seqs
        if self.gpu_allocator.get_num_total_blocks() < num_required_blocks:
            return AllocStatus.NEVER
        elif num_free_blocks - num_required_blocks >= self.watermark_blocks:
            return AllocStatus.OK
        else:
            return AllocStatus.LATER
    def can_swap_in_shared_blocks(self, seq_group: SequenceGroup) -> AllocStatus:
        blocks = self._get_physical_blocks(seq_group)

        num_swapped_seqs = seq_group.num_seqs(status=SequenceStatus.RUNNING)
        free_shared_blocks = self.gpu_allocator.get_num_free_blocks(block_type="shared")
        if free_shared_blocks < num_swapped_seqs+len(blocks):
            print(f'''Not enough free shared blocks to swap in {num_swapped_seqs} sequences. 
                  Free shared blocks: {free_shared_blocks}''')
            return False
        else:
            print("Enough free shared blocks to swap in sequences.")
            return True 

    def _swap_block_table(
            self, block_table: BlockTable, src_allocator: BlockAllocatorBase,
            dest_allocator: BlockAllocatorBase,
            mapping: Dict[PhysicalTokenBlock,
                          PhysicalTokenBlock]) -> BlockTable:
        new_block_table = []

        for from_block in block_table:
            if from_block in mapping:
                to_block = mapping[from_block]
                to_block.ref_count += 1
            else:
                to_block = dest_allocator.allocate(
                    from_block.block_hash, from_block.num_hashed_tokens)
                mapping[from_block] = to_block
            new_block_table.append(to_block)
            # Free the source block swapped in to destination.
            src_allocator.free(from_block)

        return new_block_table

    def swap_in(self, seq_group: SequenceGroup) -> List[Tuple[int, int]]:

        request_id = seq_group.request_id

        # CPU block -> GPU block.
        # dict is efficient in lookup `if cpu_block in mapping`
        mapping: Dict[PhysicalTokenBlock, PhysicalTokenBlock] = {}
        seqs = seq_group.get_seqs(status=SequenceStatus.SWAPPED) + \
               seq_group.get_seqs(status=SequenceStatus.PARTIAL_SWAPPED)
        for seq in seqs:
            seq.reset_swapped_out_block_ratio()
            block_table = self.block_tables[seq.seq_id]
            # block_device_table = self.block_device_tables[seq.seq_id]
            swapped_in_block_indices = {}
            swapping_in_blocks: List[PhysicalTokenBlock] = []
            cpu_device = Device.CPU
            # gpu_device = Device.GPU
            swapping_in_blocks_extend = swapping_in_blocks.extend
            current_length = 0
            for block_indx, block in enumerate(block_table):
                if block.device == cpu_device:
                    swapped_in_block_indices[block_indx] = current_length
                    swapping_in_blocks_extend([block])
                    current_length += 1
            if len(swapped_in_block_indices) == 0:
                swapping_in_blocks = block_table

            swapped_in_blocks = self._swap_block_table(swapping_in_blocks,
                                                       self.cpu_allocator,
                                                       self.gpu_allocator,
                                                       mapping)
            if len(swapped_in_block_indices) > 0:
                for block_indx, index_value in swapped_in_block_indices.items(
                ):
                    swapped_in_block = swapped_in_blocks[index_value]
                    self.block_tables[
                        seq.seq_id][block_indx] = swapped_in_block
            else:
                self.block_tables[seq.seq_id] = swapped_in_blocks
        # for seq in seq_group.get_seqs(status=SequenceStatus.SWAPPED):
        #     self.block_tables[seq.seq_id] = \
        #         self._swap_block_table(self.block_tables[seq.seq_id],
        #                                self.cpu_allocator,
        #                                self.gpu_allocator,
        #                                mapping)

        if seq_group.is_encoder_decoder():
            self.cross_block_tables[request_id] = \
                self._swap_block_table(self.cross_block_tables[request_id],
                                       self.cpu_allocator,
                                       self.gpu_allocator,
                                       mapping)
        return [(cpu_block.block_number, gpu_block.block_number)
                for cpu_block, gpu_block in mapping.items()]

    def can_swap_out(self, seq_group: SequenceGroup) -> bool:
        blocks = self._get_physical_blocks(seq_group)
        return len(blocks) <= self.cpu_allocator.get_num_free_blocks()

    def swap_out(self,
                 seq_group: SequenceGroup,
                 swap_out_block_nums: int = -1) -> List[Tuple[int, int]]:
        request_id = seq_group.request_id

        # GPU block -> CPU block.
        # dict is efficient in lookup `if gpu_block in mapping`
        mapping: Dict[PhysicalTokenBlock, PhysicalTokenBlock] = {}
        seqs= seq_group.get_seqs(status=SequenceStatus.RUNNING) + \
              seq_group.get_seqs(status=SequenceStatus.PARTIAL_SWAPPED)
        for seq in seqs:
            block_tables = self.block_tables[seq.seq_id]
            swapped_out_blocks_idx = 0
            swapping_out_blocks_idx = 0
            block_table_length = len(block_tables)
            if swap_out_block_nums > 0:  # Partial Swapped
                swapped_out_block_nums = seq.get_swapped_out_block_nums()
                swapped_out_blocks_idx = swapped_out_block_nums
                swapping_out_blocks_idx = min(
                    swapped_out_block_nums + swap_out_block_nums,
                    block_table_length)
                seq.update_swapped_out_block_nums(swap_out_block_nums)
                swapping_out_blocks = block_tables[
                    swapped_out_blocks_idx:swapping_out_blocks_idx]
            else:  # Swapped
                swapping_out_blocks = block_tables
                seq.update_swapped_out_block_nums(block_table_length)

            # block_device_table = self.block_device_tables[seq.seq_id]
            # swapping_out_blocks_set= set(swapping_out_blocks)
            # for block in block_tables:
            #     if block in swapping_out_blocks_set:
            #         block_device_table[block.block_number] = Device.CPU
            #     else:
            #         block_device_table[block.block_number] = Device.GPU

            swapped_out_blocks = self._swap_block_table(
                swapping_out_blocks, self.gpu_allocator, self.cpu_allocator,
                mapping)
            if swap_out_block_nums > 0:
                self.block_tables[
                    seq.seq_id][swapped_out_blocks_idx:
                                swapping_out_blocks_idx] = swapped_out_blocks
            else:
                self.block_tables[seq.seq_id] = swapped_out_blocks
            # self.block_tables[seq.seq_id] = \
            # self._swap_block_table(swapping_out_blocks,
            #    self.gpu_allocator,
            #    self.cpu_allocator,
            #    mapping)

            # self.block_device_tables[seq.seq_id] = block_device_table
        if seq_group.is_encoder_decoder():
            self.cross_block_tables[request_id] = \
                self._swap_block_table(self.cross_block_tables[request_id],
                                       self.gpu_allocator,
                                       self.cpu_allocator,
                                       mapping)

        return [(cpu_block.block_number, gpu_block.block_number)
                for cpu_block, gpu_block in mapping.items()]

    def _free_block_table(self, block_table: BlockTable, block_type: str = 'normal') -> None:
        # when using a sliding window, each seq will only use up
        # to `self.block_sliding_window` blocks. When freeing
        # the block table, we must make sure to not free blocks more
        # than once. If no sliding window is used, there is no block
        # reuse in the block table, so we must free all blocks.
        blocks_to_free = (block_table[-self.block_sliding_window:]
                          if self.block_sliding_window is not None else
                          block_table)
        for block in set(blocks_to_free):
            if block.device == Device.GPU:
                self.gpu_allocator.free(block, block_type)
            else:
                self.cpu_allocator.free(block)

    def free(self, seq: Sequence) -> None:
        seq_type = seq.get_seq_type()
        if ((seq.seq_id not in self.block_tables and seq_type == SequenceType.NORMAL) 
            or (seq.seq_id not in self.shared_block_tables and seq_type == SequenceType.TEMP)):
            # Already freed or haven't been scheduled yet.
            return
        
        if seq_type == SequenceType.TEMP:
            block_table = self.shared_block_tables[seq.seq_id]
            self._free_block_table(block_table, block_type='shared')
            del self.shared_block_tables[seq.seq_id]
        elif seq_type == SequenceType.NORMAL:
            block_table = self.block_tables[seq.seq_id]
            self._free_block_table(block_table)
            del self.block_tables[seq.seq_id]
        # del self.block_device_tables[seq.seq_id]

    def free_cross(self, seq_group: SequenceGroup) -> None:
        if seq_group.request_id not in self.cross_block_tables:
            # Already freed or hasn't ben scheduled yet.
            return
        block_table = self.cross_block_tables[seq_group.request_id]
        self._free_block_table(block_table)
        del self.cross_block_tables[seq_group.request_id]

        
    def reset_shared_blocks(self) -> None:
        # remove all shared blocks
        if len(self.shared_block_tables) == 0:
            return 
        for block_table in self.shared_block_tables.values():
            self._free_block_table(block_table, block_type='shared')
        self.shared_block_tables.clear()
        self.gpu_allocator.merge_tables()
    
    def add_shared_blocks(self, num_shared_blocks: int) -> int:
        if num_shared_blocks== 0:
            return
        shared_block_nums= min(num_shared_blocks, self.gpu_allocator.get_num_free_blocks())
        if shared_block_nums == 0:
            return 0
        else:
            allocated_shared_block_nums = self.gpu_allocator.split_tables(shared_block_nums)
            return allocated_shared_block_nums


    def reset(self) -> None:
        # Free decoder block tables
        for block_table in self.block_tables.values():
            self._free_block_table(block_table)
        # self.block_device_tables.clear()
        self.block_tables.clear()
        # Free cross-attention block tables
        for block_table in self.cross_block_tables.values():
            self._free_block_table(block_table)
        self.cross_block_tables.clear()

    def get_block_table(self, seq: Sequence, shared: bool = False) -> List[int]:
        block_table = self.block_tables[seq.seq_id] if not shared else self.shared_block_tables[seq.seq_id]
        return [block.block_number for block in block_table]
    

    def get_cross_block_table(self, seq_group: SequenceGroup) -> List[int]:
        block_table = self.cross_block_tables[seq_group.request_id]
        return [block.block_number for block in block_table]


    def get_num_free_gpu_blocks(self) -> int:
        return self.gpu_allocator.get_num_free_blocks()

    def get_num_free_cpu_blocks(self) -> int:
        return self.cpu_allocator.get_num_free_blocks()

    def access_all_blocks_in_seq(
        self,
        seq: Sequence,
        access_time: float,
    ) -> None:
        if self.enable_caching:
            # Update the last accessed time of all the blocks accessed
            # in this step.
            block_table = self.block_tables[seq.seq_id]
            for block in block_table:
                block.last_accessed = access_time

    def compute_full_blocks_in_seq(self, seq: Sequence):
        if seq.seq_id not in self.block_tables:
            return
        max_full_block = seq.get_len() // self.block_size - 1
        block_table = self.block_tables[seq.seq_id]
        if max_full_block == -1:
            return
        for i in reversed(range(max_full_block)):
            if block_table[i].computed:
                break
            block_table[i].computed = True

    def get_all_computed_blocks(self, seq: Sequence) -> List[int]:
        if seq.seq_id not in self.block_tables:
            return []
        block_table = self.block_tables[seq.seq_id]
        # NOTE We exclude the last block to avoid the case where the entire
        # prompt is cached. This would cause erroneous behavior in model
        # runner.
        return [
            b.block_number
            for b in takewhile(lambda b: b.computed, block_table[:-1])
        ]

    def get_common_computed_block_ids(
            self, seqs: List[Sequence]) -> GenericSequence[int]:
        """Return the block ids that are common for a given sequence group.

        Used in prefill (can skip prefill of some blocks).
        """
        # Can return non-empty result only with prefix caching enabled.
        if not self.enable_caching:
            return []

        ids_list = [self.get_all_computed_blocks(seq) for seq in seqs]
        return commonprefix([ids for ids in ids_list if ids != []])

    def mark_blocks_as_computed(self, seq_group: SequenceGroup):
        if self.enable_caching:
            for seq in seq_group.seqs_dict.values():
                self.compute_full_blocks_in_seq(seq)

